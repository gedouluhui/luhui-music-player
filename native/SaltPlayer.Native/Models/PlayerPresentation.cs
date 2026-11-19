namespace SaltPlayer.Native.Models;

/// <summary>One source for the home card's label, action and target.</summary>
public sealed record HomeCardAction(string Action, string Label, long? TrackId);

public static class PlayerPresentation
{
    public static HomeCardAction HomeAction(long? currentTrackId, long? recommendedTrackId, bool playing)
    {
        if (currentTrackId is not null)
            return new("toggle-current", playing ? "暂停" : "继续播放", currentTrackId);
        if (recommendedTrackId is not null)
            return new("play-recommended", "播放这首", recommendedTrackId);
        return new("add-library", "添加音乐文件夹", null);
    }

    public static bool AllowsGlobalShortcut(string key, bool editing, bool controlFocused, bool modified) =>
        !editing && !modified && key != "Delete" && (!controlFocused || key == "F1");

    public static string FormatDuration(double seconds)
    {
        if (!double.IsFinite(seconds) || seconds <= 0) return "0 分钟";
        if (seconds < 60) return "不足 1 分钟";
        // Format by division rather than TimeSpan, which throws on out-of-range input.
        var hours = Math.Floor(seconds / 3600);
        var minutes = (int)Math.Floor(seconds % 3600 / 60);
        return hours >= 1 ? $"{hours:0} 小时 {minutes} 分" : $"{minutes} 分钟";
    }
}
