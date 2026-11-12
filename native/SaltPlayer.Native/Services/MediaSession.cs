using Microsoft.UI.Dispatching;
using SaltPlayer.Native.Models;
using SaltPlayer.Native.Services;
using Windows.Media;
using Windows.Storage.Streams;

namespace SaltPlayer.Native;

/// <summary>系统媒体传输控件：媒体键、音量浮层、锁屏/任务栏的正在播放。</summary>
internal sealed class MediaSession : IDisposable
{
    private readonly SystemMediaTransportControls _smtc;
    private readonly PlayerService _player;
    private readonly DispatcherQueue _dispatcher;
    private readonly Func<long, string> _coverUrl;

    private long _metaTrackId = -1;
    private double _lastTimeline = double.NaN;
    private double _lastDuration = double.NaN;
    private bool _disposed;

    public MediaSession(IntPtr hwnd, DispatcherQueue dispatcher, PlayerService player, Func<long, string> coverUrl)
    {
        _dispatcher = dispatcher;
        _player = player;
        _coverUrl = coverUrl;

        _smtc = SystemMediaTransportControlsInterop.GetForWindow(hwnd);
        _smtc.IsEnabled = true;
        _smtc.IsPlayEnabled = true;
        _smtc.IsPauseEnabled = true;
        _smtc.IsStopEnabled = false;
        _smtc.IsNextEnabled = true;
        _smtc.IsPreviousEnabled = true;
        _smtc.IsFastForwardEnabled = false;
        _smtc.IsRewindEnabled = false;
        _smtc.ButtonPressed += OnButtonPressed;
        _smtc.PlaybackPositionChangeRequested += OnPositionRequested;
        _smtc.ShuffleEnabledChangeRequested += OnShuffleRequested;
        _smtc.AutoRepeatModeChangeRequested += OnRepeatRequested;
        _player.Changed += Sync;
        Sync();
    }

    public void Sync()
    {
        if (_disposed) return;
        try
        {
            var track = _player.Current;
            if ((track?.Id ?? -1) != _metaTrackId)
            {
                _metaTrackId = track?.Id ?? -1;
                UpdateMetadata(track);
                _lastTimeline = double.NaN;
                _lastDuration = double.NaN;
            }

            _smtc.PlaybackStatus = track is null
                ? MediaPlaybackStatus.Closed
                : _player.IsPlaying ? MediaPlaybackStatus.Playing : MediaPlaybackStatus.Paused;
            _smtc.ShuffleEnabled = _player.PlayMode == "shuffle";
            _smtc.AutoRepeatMode = _player.PlayMode switch
            {
                "one_loop" => MediaPlaybackAutoRepeatMode.Track,
                "list_loop" or "shuffle" => MediaPlaybackAutoRepeatMode.List,
                _ => MediaPlaybackAutoRepeatMode.None,
            };

            var duration = _player.Duration;
            var position = _player.Position;
            if (track is not null && duration > 0 &&
                (double.IsNaN(_lastTimeline) ||
                 Math.Abs(position - _lastTimeline) >= 0.9 ||
                 Math.Abs(duration - _lastDuration) > 0.01))
            {
                _lastTimeline = position;
                _lastDuration = duration;
                var end = TimeSpan.FromSeconds(duration);
                _smtc.UpdateTimelineProperties(new SystemMediaTransportControlsTimelineProperties
                {
                    StartTime = TimeSpan.Zero,
                    EndTime = end,
                    MinSeekTime = TimeSpan.Zero,
                    MaxSeekTime = end,
                    Position = TimeSpan.FromSeconds(Math.Clamp(position, 0, duration)),
                });
            }
        }
        catch { }
    }

    private void UpdateMetadata(Track? track)
    {
        try
        {
            var updater = _smtc.DisplayUpdater;
            if (track is null)
            {
                updater.ClearAll();
                updater.Update();
                return;
            }

            updater.Type = MediaPlaybackType.Music;
            var music = updater.MusicProperties;
            music.Title = track.TitleText;
            music.Artist = track.ArtistText;
            music.AlbumTitle = track.AlbumText;
            music.AlbumArtist = string.IsNullOrWhiteSpace(track.AlbumArtist) ? track.ArtistText : track.AlbumArtist!;
            try
            {
                updater.Thumbnail = RandomAccessStreamReference.CreateFromUri(new Uri(_coverUrl(track.Id)));
            }
            catch
            {
                updater.Thumbnail = null;
            }
            updater.Update();
        }
        catch { }
    }

    private void OnButtonPressed(SystemMediaTransportControls sender, SystemMediaTransportControlsButtonPressedEventArgs args)
    {
        _dispatcher.TryEnqueue(async () =>
        {
            try
            {
                switch (args.Button)
                {
                    case SystemMediaTransportControlsButton.Play:
                        if (!_player.IsPlaying) await _player.ToggleAsync();
                        break;
                    case SystemMediaTransportControlsButton.Pause:
                        if (_player.IsPlaying) await _player.ToggleAsync();
                        break;
                    case SystemMediaTransportControlsButton.Next:
                        await _player.NextAsync();
                        break;
                    case SystemMediaTransportControlsButton.Previous:
                        await _player.PreviousAsync();
                        break;
                }
            }
            catch { }
        });
    }

    private void OnPositionRequested(SystemMediaTransportControls sender, PlaybackPositionChangeRequestedEventArgs args)
    {
        var seconds = args.RequestedPlaybackPosition.TotalSeconds;
        _dispatcher.TryEnqueue(async () =>
        {
            try { await _player.SeekAsync(seconds); }
            catch { }
        });
    }

    private void OnShuffleRequested(SystemMediaTransportControls sender, ShuffleEnabledChangeRequestedEventArgs args)
    {
        var want = args.RequestedShuffleEnabled;
        _dispatcher.TryEnqueue(async () =>
        {
            try
            {
                if (want && _player.PlayMode != "shuffle")
                    await _player.SetPlayModeAsync("shuffle");
                else if (!want && _player.PlayMode == "shuffle")
                    await _player.SetPlayModeAsync("list_loop");
            }
            catch { }
        });
    }

    private void OnRepeatRequested(SystemMediaTransportControls sender, AutoRepeatModeChangeRequestedEventArgs args)
    {
        var mode = args.RequestedAutoRepeatMode switch
        {
            MediaPlaybackAutoRepeatMode.Track => "one_loop",
            MediaPlaybackAutoRepeatMode.List => "list_loop",
            _ => null,
        };
        if (mode is null) return;
        _dispatcher.TryEnqueue(async () =>
        {
            try { await _player.SetPlayModeAsync(mode); }
            catch { }
        });
    }

    public string DebugState()
    {
        try
        {
            var music = _smtc.DisplayUpdater.MusicProperties;
            return $"status={_smtc.PlaybackStatus} title=\"{music.Title}\" artist=\"{music.Artist}\" " +
                   $"album=\"{music.AlbumTitle}\" timeline={_lastTimeline:0.0}/{_lastDuration:0.0}";
        }
        catch (Exception e) { return "error " + e.Message; }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try
        {
            _player.Changed -= Sync;
            _smtc.ButtonPressed -= OnButtonPressed;
            _smtc.PlaybackPositionChangeRequested -= OnPositionRequested;
            _smtc.ShuffleEnabledChangeRequested -= OnShuffleRequested;
            _smtc.AutoRepeatModeChangeRequested -= OnRepeatRequested;
            _smtc.IsEnabled = false;
        }
        catch { }
    }
}
