using System.Collections.ObjectModel;
using System.Threading;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using SaltPlayer.Native.Models;

namespace SaltPlayer.Native.Services;

/// <summary>
/// Queue + transport state. The audio itself is produced by the Python native engine,
/// so this class never opens a device: it drives /api/native and mirrors its state.
/// </summary>
public sealed class PlayerService
{
    private readonly ApiClient _api;
    private readonly DispatcherQueue _dispatcher;
    private readonly DispatcherTimer _poll;
    private readonly DispatcherTimer _saveTick;
    private readonly DispatcherTimer _volumeDebounce;
    private bool _polling;
    private bool _endHandled;
    private int _forcedNext = -1;
    private int _tickCount;
    private int _toggleBusy;
    private int _playGen;
    private int _pollFails;
    private bool _shutdown;
    private bool _offlineNotified;
    private double _volume = 0.8;

    public ObservableCollection<Track> Queue { get; } = new();
    public Track? Current { get; private set; }
    public int Index { get; private set; } = -1;
    public string Context { get; private set; } = "";
    public bool IsPlaying { get; private set; }
    public bool IsBusy { get; private set; }
    public double Position { get; private set; }
    public double Duration { get; private set; }
    public bool Muted { get; private set; }
    public string PlayMode { get; private set; } = "list_loop";
    public double Volume
    {
        get => _volume;
        private set
        {
            if (Math.Abs(_volume - value) < 0.0001) return;
            _volume = Math.Clamp(value, 0, 1);
            Raise();
        }
    }

    public string? LastError { get; private set; }

    public event Action? Changed;
    public event Action<string>? ErrorOccurred;
    /// <summary>Non-fatal backend notices (exclusive fallback, device released, …).</summary>
    public event Action<string>? NoticeOccurred;
    private string? _lastNotice;

    private void RaiseNotice(string? note)
    {
        if (string.IsNullOrWhiteSpace(note))
        {
            _lastNotice = null;
            return;
        }
        if (string.Equals(note, _lastNotice, StringComparison.Ordinal)) return;
        _lastNotice = note;
        NoticeOccurred?.Invoke(note);
    }

    // 诊断迹线：SALTPLAYER_NATIVE_TRACE=<文件> 时把每次动作与轮询判定写进同一个迹线文件
    // （MainWindow.Trace 的 sink，前缀 [svc]）。环境变量为空 ⇒ 全部调用直接返回。
    private static readonly string? TracePath = Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_TRACE");

    private static void Trace(string message)
    {
        if (string.IsNullOrEmpty(TracePath)) return;
        var line = $"{DateTime.Now:HH:mm:ss.fff} [svc] {message}{Environment.NewLine}";
        for (var attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                File.AppendAllText(TracePath, line);
                return;
            }
            catch (IOException)
            {
                Thread.Sleep(5);
            }
            catch
            {
                return;
            }
        }
    }

    public PlayerService(ApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
        _poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
        _poll.Tick += async (_, _) => await PollAsync();
        _saveTick = new DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
        _saveTick.Tick += async (_, _) => await SaveQueueAsync();
        _volumeDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(220) };
        _volumeDebounce.Tick += async (_, _) =>
        {
            _volumeDebounce.Stop();
            await PushVolumeAsync();
        };
    }

    public void ApplySettings(SettingsDto s)
    {
        Volume = s.Volume;
        Muted = s.Muted;
        PlayMode = string.IsNullOrWhiteSpace(s.PlayMode) ? "list_loop" : s.PlayMode;
        Raise();
    }

    public async Task RestoreAsync(SettingsDto settings, CancellationToken ct = default)
    {
        try
        {
            var restored = await _api.GetAsync<QueueResponse>("queue", ct);
            if (restored.Queue.Count == 0) return;
            Queue.Clear();
            foreach (var t in restored.Queue) Queue.Add(t);
            Index = Math.Clamp(restored.Index, 0, Queue.Count - 1);
            Current = Queue[Index];
            Context = restored.Context ?? "";
            Position = restored.Position;
            Duration = Current.Duration ?? 0;
            Current.IsPlaying = false;
            Raise();

            if (settings.ResumeOnStart && settings.LastTrack == Current.Id)
            {
                var pos = Math.Max(0, restored.Position);
                await PlayCurrentAsync(pos).ConfigureAwait(true);
            }
        }
        catch
        {
            // A missing/old queue is not an error worth a dialog.
        }
    }

    public async Task PlayFromAsync(IReadOnlyList<Track> tracks, int index, string context)
    {
        if (tracks.Count == 0) return;
        index = Math.Clamp(index, 0, tracks.Count - 1);
        Trace($"play-from n={tracks.Count} index={index} ctx={context} id={tracks[index].Id}");
        _forcedNext = -1;
        SetCurrentFlag(false);
        Queue.Clear();
        foreach (var t in tracks) Queue.Add(t);
        Index = index;
        Context = context;
        Current = Queue[index];
        SetCurrentFlag(true);
        Position = 0;
        Duration = Current.Duration ?? 0;
        await PlayCurrentAsync(0).ConfigureAwait(true);
    }

    private async Task PlayCurrentAsync(double position)
    {
        var track = Current;
        if (track is null) return;
        // 起播请求在飞期间，后端 state 还停在上一首。轮询若照单全收，会把旧曲的 Ended
        // 当成「当前曲播完」再自动 next 一次（用的是已经切好的新 Index）⇒ 点一首却跳到
        // 别首、点下一首连跳几首。代际号让飞行中的轮询作废；_endHandled 先 disarm 自动
        // 切歌，等新曲真正起播成功（下方）再 arm。
        _playGen++;
        _endHandled = true;
        IsBusy = true;
        LastError = null;
        Raise();
        Trace($"play-start gen={_playGen} id={track.Id} pos={position:F2} dur={track.Duration ?? 0:F1} idx={Index} mode={PlayMode} paused={!IsPlaying}");
        try
        {
            var st = await _api.PostAsync<NativeState>("native", new
            {
                action = "play",
                track_id = track.Id,
                pos = position,
            }).ConfigureAwait(true);
            Trace($"play-resp ok={st.Ok} playing={st.Playing} ended={st.Ended} pos={st.Position:F2} dur={st.Duration:F2} mode={st.Mode} err={st.Error ?? "-"} note={st.NativeNote ?? "-"}");
            RaiseNotice(st.NativeNote);
            if (st.Ok == false)
            {
                LastError = st.Error ?? st.NativeNote ?? "无法开始播放";
                Trace($"play-FAIL {LastError}");
                IsPlaying = false;
                IsBusy = false;
                Raise();
                ErrorOccurred?.Invoke(LastError);
                return;
            }
            IsPlaying = true;
            IsBusy = false;
            _endHandled = false;
            Position = st.Position > 0 ? st.Position : position;
            Duration = st.Duration > 0 ? st.Duration : (track.Duration ?? 0);
            SetCurrentFlag(true);
            _poll.Interval = TimeSpan.FromMilliseconds(280);
            _poll.Start();
            _saveTick.Start();
            Raise();
            _ = _api.PostAsync("play", new { track_id = track.Id }).ContinueWith(_ => { });
            await SaveQueueAsync().ConfigureAwait(true);
        }
        catch (Exception e)
        {
            Trace($"play-EX {e.GetType().Name}: {e.Message}");
            IsBusy = false;
            IsPlaying = false;
            LastError = e.Message;
            Raise();
            ErrorOccurred?.Invoke(e.Message);
        }
    }

    public async Task ToggleAsync()
    {
        if (Current is null) return;
        Trace($"toggle playing={IsPlaying} pos={Position:F2}/{Duration:F2} busy={_toggleBusy} id={Current.Id}");
        // 防重入：恢复播放的 HTTP 还在飞时，轮询可能把 IsPlaying 拍回 false，紧接着的第二次
        // 点击会再发一条 play ⇒ 后端两条解码线程喂同一环形（2026-11-08「回声+卡顿」的触发源）。
        // 引擎侧已串行+代际门兜底；这里把重复请求挡在门口，免得用户听到一次重启空拍。
        if (Interlocked.Exchange(ref _toggleBusy, 1) != 0) return;
        try
        {
            if (IsPlaying)
            {
                IsPlaying = false;
                Raise();
                try { await _api.PostAsync<NativeState>("native", new { action = "pause" }).ConfigureAwait(true); }
                catch (Exception e) { LastError = e.Message; Raise(); }
            }
            else
            {
                if (Position >= Duration - 0.15 && Duration > 0)
                {
                    Trace("toggle-restart-at-end");
                    await PlayCurrentAsync(0).ConfigureAwait(true);
                    return;
                }
                IsPlaying = true;
                Raise();
                try
                {
                    var st = await _api.PostAsync<NativeState>("native", new
                    {
                        action = "resume",
                        track_id = Current.Id,
                    }).ConfigureAwait(true);
                    if (st.Ignored is not null)
                    {
                        // Backend no longer holds this file (restarted server): start it fresh.
                        Trace($"toggle-resume-ignored pos={st.Position:F2} ended={st.Ended} path={st.Path}");
                        await PlayCurrentAsync(Position).ConfigureAwait(true);
                        return;
                    }
                    Trace($"toggle-resume-ok pos={st.Position:F2} ended={st.Ended} playing={st.Playing}");
                    _poll.Interval = TimeSpan.FromMilliseconds(280);
                    _poll.Start();
                }
                catch (Exception e)
                {
                    IsPlaying = false;
                    LastError = e.Message;
                    Raise();
                    ErrorOccurred?.Invoke(e.Message);
                }
            }
        }
        finally
        {
            Interlocked.Exchange(ref _toggleBusy, 0);
        }
    }

    public async Task SeekAsync(double seconds)
    {
        if (Current is null) return;
        Position = Math.Clamp(seconds, 0, Duration > 0 ? Duration : seconds);
        Raise();
        Trace($"seek-to {Position:F2}/{Duration:F2} id={Current.Id} playing={IsPlaying}");
        try
        {
            var st = await _api.PostAsync<NativeState>("native", new
            {
                action = "seek",
                track_id = Current.Id,
                pos = Position,
            }).ConfigureAwait(true);
            Trace($"seek-resp ok={st.Ok} ignored={st.Ignored ?? "-"} playing={st.Playing} ended={st.Ended} pos={st.Position:F2}");
        }
        catch (Exception e)
        {
            LastError = e.Message;
            Raise();
        }
    }

    public async Task NextAsync(bool auto = false)
    {
        if (Queue.Count == 0) return;
        // 切歌请求还在飞时再点下一首/上一首，会在已推进的 Index 上再推一次 ⇒ 连跳几首。
        // 加载窗口内忽略额外的切歌请求（自动切歌同理），等新曲起播后再接受下一次。
        if (IsBusy) return;
        Trace($"next auto={auto} idx={Index} count={Queue.Count} mode={PlayMode} forced={_forcedNext} pos={Position:F2}/{Duration:F2}");
        if (auto && PlayMode == "one_loop")
        {
            await PlayCurrentAsync(0).ConfigureAwait(true);
            return;
        }
        if (_forcedNext >= 0 && _forcedNext < Queue.Count && _forcedNext != Index)
        {
            var forced = _forcedNext;
            _forcedNext = -1;
            await MoveToAsync(forced).ConfigureAwait(true);
            return;
        }
        _forcedNext = -1;
        if (PlayMode == "shuffle" && Queue.Count > 1)
        {
            var next = Random.Shared.Next(Queue.Count - 1);
            if (next >= Index) next++;
            Trace($"next-shuffle pick={next}");
            await MoveToAsync(next).ConfigureAwait(true);
            return;
        }
        if (Index + 1 < Queue.Count)
        {
            await MoveToAsync(Index + 1).ConfigureAwait(true);
            return;
        }
        if (PlayMode == "list_loop")
        {
            await MoveToAsync(0).ConfigureAwait(true);
            return;
        }
        await StopAtEndAsync().ConfigureAwait(true);
    }

    public async Task PreviousAsync()
    {
        if (Queue.Count == 0) return;
        if (IsBusy) return;
        Trace($"prev idx={Index} pos={Position:F2} count={Queue.Count} mode={PlayMode}");
        if (Position > 3)
        {
            await SeekAsync(0).ConfigureAwait(true);
            return;
        }
        if (PlayMode == "shuffle" && Queue.Count > 1)
        {
            var prev = Random.Shared.Next(Queue.Count - 1);
            if (prev >= Index) prev++;
            await MoveToAsync(prev).ConfigureAwait(true);
            return;
        }
        var target = Index > 0 ? Index - 1 : Queue.Count - 1;
        await MoveToAsync(target).ConfigureAwait(true);
    }

    public void Rebind(Func<Track, Track> map)
    {
        for (var i = 0; i < Queue.Count; i++) Queue[i] = map(Queue[i]);
        if (Queue.Count == 0) return;
        Index = Math.Clamp(Index, 0, Queue.Count - 1);
        Current = Queue[Index];
        Current.IsPlaying = IsPlaying;
        Raise();
    }

    public Task PlayAtAsync(int index) => MoveToAsync(index);

    // 插队：随机模式下 Index+1 并不保证是下一首，用 _forcedNext 钉住它。
    public async Task PlayNextAsync(Track track)
    {
        if (Queue.Count == 0)
        {
            await PlayFromAsync(new[] { track }, 0, "插队播放").ConfigureAwait(true);
            return;
        }
        var at = Index + 1;
        Queue.Insert(at, track);
        _forcedNext = at;
        Raise();
        await SaveQueueAsync().ConfigureAwait(true);
    }

    public async Task EnqueueAsync(Track track)
    {
        if (Queue.Count == 0)
        {
            await PlayFromAsync(new[] { track }, 0, "队列").ConfigureAwait(true);
            return;
        }
        Queue.Add(track);
        Raise();
        await SaveQueueAsync().ConfigureAwait(true);
    }

    public async Task RemoveAtAsync(int index)
    {
        if (index < 0 || index >= Queue.Count) return;
        Trace($"remove-at {index} cur={Index} count={Queue.Count} playing={IsPlaying} id={Queue[index].Id}");
        if (_forcedNext == index) _forcedNext = -1;
        else if (_forcedNext > index) _forcedNext--;

        if (index != Index)
        {
            Queue.RemoveAt(index);
            if (index < Index) Index--;
            Raise();
            await SaveQueueAsync().ConfigureAwait(true);
            return;
        }

        var wasPlaying = IsPlaying;
        SetCurrentFlag(false);
        Queue.RemoveAt(index);
        if (Queue.Count == 0)
        {
            Current = null;
            Index = -1;
            Context = "";
            Position = 0;
            Duration = 0;
            await StopAtEndAsync().ConfigureAwait(true);
            await SaveQueueAsync().ConfigureAwait(true);
            return;
        }
        Index = Math.Min(index, Queue.Count - 1);
        Current = Queue[Index];
        Position = 0;
        Duration = Current.Duration ?? 0;
        if (wasPlaying)
        {
            SetCurrentFlag(true);
            await PlayCurrentAsync(0).ConfigureAwait(true);
        }
        else
        {
            // 暂停中移除当前曲：不起流。下次 resume 时后端发现 track 对不上会走 Ignored 重开。
            Raise();
            await SaveQueueAsync().ConfigureAwait(true);
        }
    }

    public async Task ClearQueueAsync(bool keepCurrent = true)
    {
        if (Queue.Count == 0) return;
        _forcedNext = -1;
        if (keepCurrent && Current is not null)
        {
            var current = Current;
            var playing = IsPlaying;
            Queue.Clear();
            Queue.Add(current);
            Index = 0;
            Current = current;
            Current.IsPlaying = playing;
            Raise();
            await SaveQueueAsync().ConfigureAwait(true);
            return;
        }
        SetCurrentFlag(false);
        Queue.Clear();
        Current = null;
        Index = -1;
        Context = "";
        Position = 0;
        Duration = 0;
        await StopAtEndAsync().ConfigureAwait(true);
        await SaveQueueAsync().ConfigureAwait(true);
    }

    public Task FlushAsync() => SaveQueueAsync();

    private async Task MoveToAsync(int index)
    {
        if (index < 0 || index >= Queue.Count) return;
        Trace($"move-to {index} id={Queue[index].Id} from={Index} playMode={PlayMode}");
        _forcedNext = -1;
        SetCurrentFlag(false);
        Index = index;
        Current = Queue[index];
        SetCurrentFlag(true);
        Position = 0;
        Duration = Current.Duration ?? 0;
        await PlayCurrentAsync(0).ConfigureAwait(true);
    }

    private async Task StopAtEndAsync()
    {
        Trace("stop-at-end");
        IsPlaying = false;
        try { await _api.PostAsync<NativeState>("native", new { action = "stop" }).ConfigureAwait(true); }
        catch { }
        _poll.Stop();
        Raise();
    }

    private async Task PollAsync()
    {
        if (_polling || Current is null || IsBusy) return;
        _polling = true;
        var gen = _playGen;
        try
        {
            var st = await _api.GetAsync<NativeState>("native/state").ConfigureAwait(true);
            _pollFails = 0;
            _offlineNotified = false;
            // GET 飞行期间用户点了歌/切了歌：这份 state 还停在上一首，照收会镜像错曲目，
            // 还会拿旧曲的 Ended 触发自动 next（用的是已切好的新 Index）⇒ 跳到别首。
            // 代际变了或正在起播，就把这份陈旧快照作废。
            if (gen != _playGen || IsBusy) return;
            if (st.Ok == false)
            {
                Trace($"poll-notok err={st.Error}");
                return;
            }
            Trace($"poll playing={st.Playing} ended={st.Ended} pos={st.Position:F2}/{st.Duration:F2} underruns={st.Underruns} mode={st.Mode} note={st.NativeNote ?? "-"}");
            RaiseNotice(st.NativeNote);
            var wasPlaying = IsPlaying;
            IsPlaying = st.Playing && !st.Ended;
            Position = Math.Max(0, st.Position);
            if (st.Duration > 0) Duration = st.Duration;
            if (!IsPlaying && wasPlaying && st.Ended && !_endHandled && !IsBusy)
            {
                Trace("ended-edge auto-next");
                _endHandled = true;
                _poll.Interval = TimeSpan.FromMilliseconds(900);
                Raise();
                await NextAsync(auto: true).ConfigureAwait(true);
                return;
            }
            if (!IsPlaying && !st.Ended)
            {
                _poll.Interval = TimeSpan.FromMilliseconds(900);
            }
            Raise();

            _tickCount++;
            if (_tickCount % 4 == 0)
            {
                await ReportPlaybackAsync(st).ConfigureAwait(true);
            }
        }
        catch
        {
            // backend restarting or shutting down: keep the last mirrored state
            if (!_shutdown && ++_pollFails >= 6 && !_offlineNotified)
            {
                // 后端真的挂了时不能只是界面定格：给一次明确反馈，而不是让用户以为还在播。
                _offlineNotified = true;
                LastError = "音频服务无响应，播放可能已中断。设置 → 关于 → 打开后端日志可排查。";
                ErrorOccurred?.Invoke(LastError);
            }
        }
        finally
        {
            _polling = false;
        }
    }

    private Task ReportPlaybackAsync(NativeState st)
    {
        var t = Current;
        if (t is null) return Task.CompletedTask;
        return _api.PostAsync("playback", new
        {
            track_id = t.Id,
            position = st.Position,
            playing = st.Playing,
            rate = st.Speed > 0 ? st.Speed : 1,
            duration = st.Duration > 0 ? st.Duration : (double?)null,
        }).ContinueWith(_ => { });
    }

    private async Task SaveQueueAsync()
    {
        if (Queue.Count == 0) return;
        try
        {
            await _api.PostAsync("queue", new
            {
                items = Queue.Select(t => t.Id).ToArray(),
                index = Index,
                context = Context,
                track_id = Current?.Id,
                position = Position,
            }).ConfigureAwait(true);
        }
        catch
        {
        }
    }

    public void SetVolume(double value)
    {
        Volume = value;
        _volumeDebounce.Stop();
        _volumeDebounce.Start();
    }

    public void ToggleMute()
    {
        Muted = !Muted;
        Raise();
        _volumeDebounce.Stop();
        _volumeDebounce.Start();
    }

    private async Task PushVolumeAsync()
    {
        try
        {
            await _api.PostAsync("settings", new
            {
                settings = new Dictionary<string, object>
                {
                    ["volume"] = Volume,
                    ["muted"] = Muted,
                },
            }).ConfigureAwait(true);
            await _api.PostAsync<NativeState>("native", new { action = "configure" }).ConfigureAwait(true);
        }
        catch
        {
        }
    }

    public async Task SetPlayModeAsync(string mode)
    {
        PlayMode = mode;
        Raise();
        try
        {
            await _api.PostAsync("settings", new { settings = new Dictionary<string, object> { ["play_mode"] = mode } })
                .ConfigureAwait(true);
        }
        catch
        {
        }
    }

    // 倍速是 play() 的开流参数（ffmpeg atempo），configure 动不了已开的流。
    // 正在播就从当前位置重开一条流接上；没在播只落盘，下次起播自然带新速度。
    public async Task RestartCurrentAsync()
    {
        if (Current is null || !IsPlaying) return;
        Trace($"restart-current pos={Position:F2} id={Current.Id}");
        await PlayCurrentAsync(Position).ConfigureAwait(true);
    }

    public async Task ToggleFavoriteAsync(Track track)
    {
        try
        {
            var r = await _api.PostAsync<FavoriteResponse>("favorites/toggle", new { track_id = track.Id }).ConfigureAwait(true);
            track.Favorite = r.Favorite;
        }
        catch (Exception e)
        {
            ErrorOccurred?.Invoke(e.Message);
        }
    }

    private void SetCurrentFlag(bool on)
    {
        if (Current is not null) Current.IsPlaying = on;
    }

    private void Raise()
    {
        if (_dispatcher.HasThreadAccess) Changed?.Invoke();
        else _dispatcher.TryEnqueue(() => Changed?.Invoke());
    }

    public void Shutdown()
    {
        _shutdown = true;
        _poll.Stop();
        _saveTick.Stop();
        _volumeDebounce.Stop();
        _ = SaveQueueAsync();
    }
}
