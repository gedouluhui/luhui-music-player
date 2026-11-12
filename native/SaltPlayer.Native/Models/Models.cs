using System.ComponentModel;
using System.Globalization;
using System.Runtime.CompilerServices;
using System.Text.Json.Serialization;

namespace SaltPlayer.Native.Models;

public abstract class ObservableObject : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;

    protected bool Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value;
        Raise(name);
        return true;
    }

    protected void Raise([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

public sealed class AppInfo
{
    [JsonPropertyName("name")] public string Name { get; set; } = "LuHui音乐播放器";
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("native")] public bool Native { get; set; }
}

public sealed class WindowPlacementDto
{
    [JsonPropertyName("w")] public int W { get; set; } = 1320;
    [JsonPropertyName("h")] public int H { get; set; } = 830;
    [JsonPropertyName("x")] public int? X { get; set; }
    [JsonPropertyName("y")] public int? Y { get; set; }
    [JsonPropertyName("maximized")] public bool Maximized { get; set; }
}

public sealed class MiniWindowDto
{
    [JsonPropertyName("x")] public int? X { get; set; }
    [JsonPropertyName("y")] public int? Y { get; set; }
    [JsonPropertyName("w")] public int W { get; set; } = 460;
    [JsonPropertyName("h")] public int H { get; set; } = 68;
}

public sealed class SettingsDto
{
    [JsonPropertyName("theme")] public string Theme { get; set; } = "dark";
    [JsonPropertyName("window")] public WindowPlacementDto Window { get; set; } = new();
    [JsonPropertyName("desktop_lyric")] public bool DesktopLyric { get; set; }
    [JsonPropertyName("desktop_lyric_on_top")] public bool DesktopLyricOnTop { get; set; } = true;
    [JsonPropertyName("desktop_lyric_scale")] public double DesktopLyricScale { get; set; } = 1.0;
    [JsonPropertyName("mini_window")] public MiniWindowDto MiniWindow { get; set; } = new();
    [JsonPropertyName("play_mode")] public string PlayMode { get; set; } = "list_loop";
    [JsonPropertyName("volume")] public double Volume { get; set; } = 0.8;
    [JsonPropertyName("muted")] public bool Muted { get; set; }
    [JsonPropertyName("rate")] public double Rate { get; set; } = 1.0;
    [JsonPropertyName("last_track")] public long? LastTrack { get; set; }
    [JsonPropertyName("last_position")] public double LastPosition { get; set; }
    [JsonPropertyName("resume_on_start")] public bool ResumeOnStart { get; set; } = true;
    [JsonPropertyName("close_to_tray")] public bool CloseToTray { get; set; } = true;
    [JsonPropertyName("audio_engine")] public string AudioEngine { get; set; } = "chromium";
    [JsonPropertyName("audio_exclusive")] public bool AudioExclusive { get; set; }
    [JsonPropertyName("audio_device")] public int? AudioDevice { get; set; }
    [JsonPropertyName("audio_target_rate")] public int AudioTargetRate { get; set; }
    [JsonPropertyName("audio_balance")] public double AudioBalance { get; set; }
    [JsonPropertyName("audio_loudness")] public bool AudioLoudness { get; set; }
    [JsonPropertyName("audio_fade_in")] public double AudioFadeIn { get; set; }
    [JsonPropertyName("audio_speakers")] public bool AudioSpeakers { get; set; }
    [JsonPropertyName("audio_speakers_amount")] public double AudioSpeakersAmount { get; set; } = 0.35;
    [JsonPropertyName("audio_dither")] public bool AudioDither { get; set; } = true;
    [JsonPropertyName("include_unsupported")] public bool IncludeUnsupported { get; set; }
    [JsonPropertyName("eq_enabled")] public bool EqEnabled { get; set; }
    [JsonPropertyName("eq_gains")] public double[] EqGains { get; set; } = new double[10];
    [JsonPropertyName("eq_preset")] public string EqPreset { get; set; } = "flat";
    [JsonPropertyName("lyric_offset")] public double LyricOffset { get; set; }
    [JsonPropertyName("lyric_size")] public double LyricSize { get; set; } = 100;
    [JsonPropertyName("lyric_mode")] public string LyricMode { get; set; } = "scroll";
    [JsonPropertyName("lyric_scroll_ms")] public double LyricScrollMs { get; set; }
    [JsonPropertyName("show_translation")] public bool ShowTranslation { get; set; } = true;
    [JsonPropertyName("playbar_lyric")] public bool PlaybarLyric { get; set; } = true;
    [JsonPropertyName("gapless")] public bool Gapless { get; set; } = true;
    [JsonPropertyName("crossfade_sec")] public double CrossfadeSec { get; set; }
    [JsonPropertyName("ambient")] public bool Ambient { get; set; } = true;
    [JsonPropertyName("last_view")] public string? LastView { get; set; }
}

public sealed class LibraryStats
{
    [JsonPropertyName("tracks")] public int Tracks { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("artists")] public int Artists { get; set; }
    [JsonPropertyName("albums")] public int Albums { get; set; }
    [JsonPropertyName("favorites")] public int Favorites { get; set; }
    [JsonPropertyName("plays")] public int Plays { get; set; }
}

public sealed class LibraryInfo
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("name")] public string? Name { get; set; }
    [JsonPropertyName("added_at")] public double AddedAt { get; set; }
    [JsonPropertyName("last_scan")] public double? LastScan { get; set; }
    [JsonPropertyName("track_count")] public int TrackCount { get; set; }

    public string DisplayName => string.IsNullOrWhiteSpace(Name)
        ? (string.IsNullOrWhiteSpace(Path) ? "音乐文件夹" : System.IO.Path.GetFileName(Path.TrimEnd('\\', '/')))
        : Name!;
}

public sealed class PlaylistInfo
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("track_count")] public int TrackCount { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("cover_track")] public long? CoverTrack { get; set; }
    [JsonPropertyName("pinned")] public int Pinned { get; set; }
    [JsonPropertyName("sort_order")] public int SortOrder { get; set; }

    [JsonIgnore] public string CountText => TrackCount.ToString();
}

public sealed class BootstrapResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("app")] public AppInfo App { get; set; } = new();
    [JsonPropertyName("settings")] public SettingsDto Settings { get; set; } = new();
    [JsonPropertyName("libraries")] public List<LibraryInfo> Libraries { get; set; } = new();
    [JsonPropertyName("playlists")] public List<PlaylistInfo> Playlists { get; set; } = new();
    [JsonPropertyName("stats")] public LibraryStats Stats { get; set; } = new();
}

public sealed class Track : ObservableObject
{
    private bool _favorite;
    private bool _isPlaying;

    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("title")] public string? Title { get; set; }
    [JsonPropertyName("artist")] public string? Artist { get; set; }
    [JsonPropertyName("album")] public string? Album { get; set; }
    [JsonPropertyName("album_artist")] public string? AlbumArtist { get; set; }
    [JsonPropertyName("genre")] public string? Genre { get; set; }
    [JsonPropertyName("year")] public int? Year { get; set; }
    [JsonPropertyName("track_no")] public int? TrackNo { get; set; }
    [JsonPropertyName("disc_no")] public int? DiscNo { get; set; }
    [JsonPropertyName("duration")] public double? Duration { get; set; }
    [JsonPropertyName("bitrate")] public int? Bitrate { get; set; }
    [JsonPropertyName("sample_rate")] public int? SampleRate { get; set; }
    [JsonPropertyName("channels")] public int? Channels { get; set; }
    [JsonPropertyName("fmt")] public string? Fmt { get; set; }
    [JsonPropertyName("size")] public long? Size { get; set; }
    [JsonPropertyName("mtime")] public double? Mtime { get; set; }
    [JsonPropertyName("folder")] public string? Folder { get; set; }
    [JsonPropertyName("filename")] public string? Filename { get; set; }
    [JsonPropertyName("path")] public string? Path { get; set; }
    [JsonPropertyName("bit_depth")] public int? BitDepth { get; set; }
    [JsonPropertyName("playable")] public int Playable { get; set; } = 1;
    [JsonPropertyName("has_cover")] public int HasCover { get; set; }
    [JsonPropertyName("library_id")] public long? LibraryId { get; set; }
    [JsonPropertyName("added_at")] public double? AddedAt { get; set; }
    [JsonPropertyName("missing")] public int Missing { get; set; }
    [JsonPropertyName("has_lyric")] public int HasLyric { get; set; }
    [JsonPropertyName("favorite")] public bool Favorite
    {
        get => _favorite;
        set
        {
            if (!Set(ref _favorite, value)) return;
            Raise(nameof(FavoriteOn));
            Raise(nameof(FavoriteOff));
            Raise(nameof(FavoriteGlyph));
        }
    }

    [JsonIgnore] public bool IsPlaying
    {
        get => _isPlaying;
        set
        {
            if (!Set(ref _isPlaying, value)) return;
            Raise(nameof(RowNumberVisible));
            Raise(nameof(PlayingBadgeVisible));
            Raise(nameof(PlayingTintVisible));
        }
    }

    [JsonIgnore] public string TitleText =>
        string.IsNullOrWhiteSpace(Title)
            ? (string.IsNullOrWhiteSpace(Filename) ? "未知曲目" : System.IO.Path.GetFileNameWithoutExtension(Filename))
            : Title!;

    [JsonIgnore] public string ArtistText =>
        string.IsNullOrWhiteSpace(Artist) ? "未知艺术家" : Artist!;

    [JsonIgnore] public string AlbumText =>
        string.IsNullOrWhiteSpace(Album) ? "未知专辑" : Album!;

    [JsonIgnore] public string DurationText
    {
        get
        {
            var d = Duration ?? 0;
            if (d <= 0) return "--:--";
            var ts = TimeSpan.FromSeconds(d);
            return ts.TotalHours >= 1
                ? $"{(int)ts.TotalHours}:{ts.Minutes:00}:{ts.Seconds:00}"
                : $"{ts.Minutes}:{ts.Seconds:00}";
        }
    }

    [JsonIgnore] public string NumberText => RowNumber > 0
        ? RowNumber.ToString(CultureInfo.InvariantCulture)
        : (TrackNo is > 0 ? TrackNo.Value.ToString(CultureInfo.InvariantCulture) : "");

    [JsonIgnore] public int RowNumber { get; set; }

    [JsonIgnore] public string FormatBadge =>
        string.IsNullOrWhiteSpace(Fmt) ? "" : Fmt!.ToUpperInvariant();

    [JsonIgnore] public string QualityDetail
    {
        get
        {
            var parts = new List<string>(2);
            if (SampleRate is > 0) parts.Add($"{SampleRate.Value / 1000.0:0.#} kHz");
            if (Bitrate is > 0) parts.Add($"{Bitrate.Value / 1000} kbps");
            return string.Join(" · ", parts);
        }
    }

    [JsonIgnore] public string CoverUrl { get; set; } = "";
    [JsonIgnore] public string TooltipText => $"{TitleText}\n{ArtistText} · {AlbumText}";

    // 不缓存 BitmapImage：某次加载失败（后端并发生成缩略图撞车等）时，x:Bind
    // 重新实现这一行会重新取值并重试，而不是把失败对象永久留在曲目上。
    [JsonIgnore] public Microsoft.UI.Xaml.Media.ImageSource? CoverImage =>
        AlbumInfo.LoadCover(CoverUrl, 96);

    // 没内嵌封面的歌，后端也会按专辑/艺人生成一张首字色块图；只要有地址就显示图片，
    // 不再各视图各挂一个音符占位块（首页卡片和播放条因此长得一致）。
    [JsonIgnore] public Microsoft.UI.Xaml.Visibility ArtVisible =>
        string.IsNullOrWhiteSpace(CoverUrl)
            ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility ArtPlaceholderVisible =>
        string.IsNullOrWhiteSpace(CoverUrl)
            ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility FavoriteOn =>
        Favorite ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility FavoriteOff =>
        Favorite ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;

    [JsonIgnore] public string FavoriteGlyph => Favorite ? "\uE735" : "\uE734";

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility RowNumberVisible =>
        IsPlaying ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility PlayingBadgeVisible =>
        IsPlaying ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility PlayingTintVisible =>
        IsPlaying ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    public void RefreshFavorite() => Raise(nameof(Favorite));
}

public sealed class AlbumInfo
{
    [JsonPropertyName("album")] public string Album { get; set; } = "";
    [JsonPropertyName("artist")] public string? Artist { get; set; }
    [JsonPropertyName("tracks")] public int Tracks { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("year")] public int? Year { get; set; }
    [JsonPropertyName("cover_track")] public long? CoverTrack { get; set; }
    [JsonPropertyName("genre")] public string? Genre { get; set; }
    [JsonPropertyName("idx")] public string? Idx { get; set; }
    [JsonPropertyName("ids")] public List<long>? Ids { get; set; }

    [JsonIgnore] public string CoverUrl { get; set; } = "";
    [JsonIgnore] public string ArtistText => string.IsNullOrWhiteSpace(Artist) ? "未知艺术家" : Artist!;
    [JsonIgnore] public string Subtitle => Year is > 0 ? $"{ArtistText} · {Year}" : ArtistText;

    [JsonIgnore] public Microsoft.UI.Xaml.Media.ImageSource? CoverImage =>
        LoadCover(CoverUrl, 360);

    internal static Microsoft.UI.Xaml.Media.ImageSource? LoadCover(string url, int decodeWidth)
    {
        if (string.IsNullOrWhiteSpace(url)) return null;
        return new Microsoft.UI.Xaml.Media.Imaging.BitmapImage
        {
            UriSource = new Uri(url),
            DecodePixelWidth = decodeWidth,
        };
    }

    [JsonIgnore] public string MetaText
    {
        get
        {
            var ts = TimeSpan.FromSeconds(Duration);
            var len = ts.TotalHours >= 1 ? $"{(int)ts.TotalHours} 小时 {ts.Minutes} 分" : $"{ts.Minutes} 分钟";
            return $"{Tracks} 首 · {len}";
        }
    }
}

public sealed class ArtistInfo
{
    [JsonPropertyName("artist")] public string Name { get; set; } = "";
    [JsonPropertyName("tracks")] public int Tracks { get; set; }
    [JsonPropertyName("albums")] public int Albums { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("cover_track")] public long? CoverTrack { get; set; }
    [JsonPropertyName("idx")] public string? Idx { get; set; }
    [JsonPropertyName("genre")] public string? Genre { get; set; }

    [JsonIgnore] public string CoverUrl { get; set; } = "";
    [JsonIgnore] public string MetaText => $"{Albums} 张专辑 · {Tracks} 首";

    [JsonIgnore] public Microsoft.UI.Xaml.Media.ImageSource? CoverImage =>
        AlbumInfo.LoadCover(CoverUrl, 360);
}

public sealed class TracksResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<Track> Items { get; set; } = new();
    [JsonPropertyName("total")] public int Total { get; set; }
}

public sealed class AlbumsResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<AlbumInfo> Items { get; set; } = new();
    [JsonPropertyName("total")] public int Total { get; set; }
}

public sealed class ArtistsResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<ArtistInfo> Items { get; set; } = new();
    [JsonPropertyName("total")] public int Total { get; set; }
}

public sealed class QueueResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("queue")] public List<Track> Queue { get; set; } = new();
    [JsonPropertyName("index")] public int Index { get; set; }
    [JsonPropertyName("context")] public string? Context { get; set; }
    [JsonPropertyName("track_id")] public long? TrackId { get; set; }
    [JsonPropertyName("position")] public double Position { get; set; }
}

public sealed class PlaylistsResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<PlaylistInfo> Items { get; set; } = new();
    [JsonPropertyName("id")] public long Id { get; set; }
}

public sealed class TrackResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("track")] public Track? Track { get; set; }
}

public sealed class M3uExportResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("path")] public string? Path { get; set; }
    [JsonPropertyName("count")] public int Count { get; set; }
    [JsonPropertyName("missing")] public int Missing { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

public sealed class ExcludesResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<string> Items { get; set; } = new();
    [JsonPropertyName("already")] public bool Already { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

public sealed class ScanStatusResponse
{
    [JsonPropertyName("running")] public bool Running { get; set; }
    [JsonPropertyName("phase")] public string? Phase { get; set; }
    [JsonPropertyName("total")] public int Total { get; set; }
    [JsonPropertyName("done")] public int Done { get; set; }
    [JsonPropertyName("current")] public string? Current { get; set; }
    [JsonPropertyName("added")] public int Added { get; set; }
    [JsonPropertyName("updated")] public int Updated { get; set; }
    [JsonPropertyName("removed")] public int Removed { get; set; }
    [JsonPropertyName("skipped")] public int Skipped { get; set; }
    [JsonPropertyName("excluded")] public int Excluded { get; set; }
    [JsonPropertyName("started_at")] public double StartedAt { get; set; }
    [JsonPropertyName("finished_at")] public double FinishedAt { get; set; }
}

public sealed class PurgeResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("removed")] public int Removed { get; set; }
}

public sealed class ScanStartResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("started")] public bool Started { get; set; }
    [JsonPropertyName("busy")] public bool Busy { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

public sealed class NativeState
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("engine")] public string? Engine { get; set; }
    [JsonPropertyName("path")] public string? Path { get; set; }
    [JsonPropertyName("playing")] public bool Playing { get; set; }
    [JsonPropertyName("position")] public double Position { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("rate")] public double Rate { get; set; }
    [JsonPropertyName("channels")] public int Channels { get; set; }
    [JsonPropertyName("out_rate")] public int OutRate { get; set; }
    [JsonPropertyName("ended")] public bool Ended { get; set; }
    [JsonPropertyName("mode")] public string? Mode { get; set; }
    [JsonPropertyName("native_note")] public string? NativeNote { get; set; }
    [JsonPropertyName("speed")] public double Speed { get; set; } = 1;
    [JsonPropertyName("atempo")] public double Atempo { get; set; } = 1;
    [JsonPropertyName("source_bits")] public int SourceBits { get; set; }
    [JsonPropertyName("source_rate")] public int SourceRate { get; set; }
    [JsonPropertyName("source_ch")] public int SourceCh { get; set; }
    [JsonPropertyName("underruns")] public int Underruns { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
    [JsonPropertyName("ignored")] public string? Ignored { get; set; }
}

public sealed class FavoriteResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("favorite")] public bool Favorite { get; set; }
}

public sealed class AudioDevice
{
    [JsonPropertyName("index")] public int Index { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("rate")] public int Rate { get; set; }
    [JsonPropertyName("channels")] public int Channels { get; set; }
    [JsonPropertyName("is_default")] public bool IsDefault { get; set; }
}

public sealed class DevicesResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("available")] public bool Available { get; set; }
    [JsonPropertyName("items")] public List<AudioDevice> Items { get; set; } = new();
}

public sealed class SettingsResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("settings")] public SettingsDto Settings { get; set; } = new();
}

public sealed class OkResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

public sealed class IndexResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("total")] public int Total { get; set; }
    [JsonPropertyName("blocks")] public List<IndexBlock> Blocks { get; set; } = new();
}

public sealed class IndexBlock
{
    [JsonPropertyName("letter")] public string Letter { get; set; } = "";
    [JsonPropertyName("offset")] public int Offset { get; set; }
    [JsonPropertyName("count")] public int Count { get; set; }
}

public sealed class AzLetter : ObservableObject
{
    public string Letter { get; init; } = "";
    public int Offset { get; init; }
    public int Count { get; init; }

    [JsonIgnore] public double Opacity => Count > 0 ? 1 : 0.26;

    private bool _active;
    [JsonIgnore] public bool Active
    {
        get => _active;
        set
        {
            if (Set(ref _active, value)) Raise(nameof(ActiveVisible));
        }
    }

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility ActiveVisible =>
        Active ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;
}

public sealed class FolderInfo
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("tracks")] public int Tracks { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("cover_track")] public long CoverTrack { get; set; }
}

public sealed class GenreInfo
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("tracks")] public int Tracks { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
}

public sealed class CategoryInfo
{
    public string Title { get; init; } = "";
    public string Subtitle { get; init; } = "";
    public string Kind { get; init; } = "";
    public string Key { get; init; } = "";
    public string Icon { get; init; } = "\uE8B7";
    public string CoverUrl { get; set; } = "";

    [JsonIgnore] public Microsoft.UI.Xaml.Media.ImageSource? CoverImage =>
        AlbumInfo.LoadCover(CoverUrl, 360);
}

public sealed class LyricLine : ObservableObject
{
    // 激活/过去/未来三态只影响颜色与不透明度，不动字号：
    // 行高随字号变化会让整屏歌词在每次换行时重排，滚动目标也跟着抖。
    private bool _active;
    private bool _past;
    private bool _showTrans = true;

    [JsonPropertyName("t")] public double T { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("trans")] public string? Trans { get; set; }

    [JsonIgnore]
    public bool Active
    {
        get => _active;
        set
        {
            if (!Set(ref _active, value)) return;
            Raise(nameof(TextBrush));
            Raise(nameof(TextOpacity));
            Raise(nameof(ActiveVisible));
        }
    }

    [JsonIgnore]
    public bool Past
    {
        get => _past;
        set
        {
            if (!Set(ref _past, value)) return;
            Raise(nameof(TextBrush));
            Raise(nameof(TextOpacity));
            Raise(nameof(TransOpacity));
        }
    }

    [JsonIgnore]
    public bool ShowTrans
    {
        get => _showTrans;
        set
        {
            if (Set(ref _showTrans, value)) Raise(nameof(TransVisible));
        }
    }

    [JsonIgnore] public double TextSize { get; set; } = 17;
    [JsonIgnore] public double TransSize => Math.Round(Math.Max(11, TextSize * 0.68), 1);
    [JsonIgnore] public double TextOpacity => _active ? 1.0 : _past ? 0.46 : 0.8;
    [JsonIgnore] public double TransOpacity => _active ? 0.9 : _past ? 0.36 : 0.56;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility ActiveVisible =>
        _active ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [JsonIgnore] public Microsoft.UI.Xaml.Visibility TransVisible =>
        _showTrans && !string.IsNullOrWhiteSpace(Trans)
            ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    [JsonIgnore]
    public Microsoft.UI.Xaml.Media.Brush TextBrush =>
        _active ? Res("TextPrimaryBrush") : Res("TextSecondaryBrush");

    [JsonIgnore] public Microsoft.UI.Xaml.Media.Brush TransBrush => Res("TextTertiaryBrush");

    private static Microsoft.UI.Xaml.Media.Brush Res(string key) =>
        (Microsoft.UI.Xaml.Media.Brush)Microsoft.UI.Xaml.Application.Current.Resources[key];
}

public sealed class LyricsResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("plain")] public bool Plain { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("source")] public string? Source { get; set; }
    [JsonPropertyName("lines")] public List<LyricLine> Lines { get; set; } = new();
}

public sealed class LyricEditResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("path")] public string? Path { get; set; }
    [JsonPropertyName("lines")] public int Lines { get; set; }
    [JsonPropertyName("source")] public string? Source { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

public sealed class FoldersResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<FolderInfo> Items { get; set; } = new();
}

public sealed class GenresResponse
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("items")] public List<GenreInfo> Items { get; set; } = new();
}
