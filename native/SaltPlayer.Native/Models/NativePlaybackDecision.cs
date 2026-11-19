namespace SaltPlayer.Native.Models;

/// <summary>Only clean completion may advance the queue; a decoder error is never EOF.</summary>
public sealed record NativePlaybackDecision(bool IsPlaying, bool NaturalEnd, string? Error)
{
    public static NativePlaybackDecision From(NativeState state)
    {
        var error = string.IsNullOrWhiteSpace(state.Error) ? null : state.Error;
        if (!state.Ok)
            error ??= string.IsNullOrWhiteSpace(state.NativeNote) ? "音频服务无法完成播放请求" : state.NativeNote;
        if (error is null && (!double.IsFinite(state.Position) || state.Position < 0
            || !double.IsFinite(state.Duration) || state.Duration < 0))
            error = "音频服务返回了无效播放位置，请重试当前曲目";
        return error is not null
            ? new(false, false, error)
            : new(state.Playing && !state.Ended, state.Ended, null);
    }
}
