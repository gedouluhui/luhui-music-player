using System.Text.Json.Serialization;

namespace SaltPlayer.Native.Models;

/// <summary>Missing protocol declarations are legacy v0; declared protocols are validated strictly.</summary>
public sealed class ApiProtocol
{
    [JsonPropertyName("major"), JsonRequired, JsonNumberHandling(JsonNumberHandling.Strict)]
    public int Major { get; set; }
    [JsonPropertyName("minor"), JsonRequired, JsonNumberHandling(JsonNumberHandling.Strict)]
    public int Minor { get; set; }
    [JsonPropertyName("capabilities"), JsonRequired]
    public List<string>? Capabilities { get; set; }

    public string? GetCompatibilityError()
    {
        if (Major != 1)
            return $"音频服务协议不兼容（主版本 {Major}）。请使用同一发行包中的界面和后端。";
        if (Minor < 0 || Capabilities is null || Capabilities.Any(string.IsNullOrWhiteSpace))
            return "音频服务协议声明无效。请检查界面和后端是否来自同一发行包。";
        // Additive minor versions and unknown capability names remain forward-compatible.
        return null;
    }
}
