using System.Text.Json;
using System.Text.Json.Serialization;

namespace SaltPlayer.Native.Models;

/// <summary>Wire contract shared by playback and pure .NET regression tests.</summary>
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
    [JsonPropertyName("error"), JsonConverter(typeof(NativeErrorConverter))]
    public string? Error { get; set; }
    [JsonPropertyName("ignored")] public string? Ignored { get; set; }
}

/// <summary>Accept legacy strings and current structured decode errors, never discard an error object.</summary>
public sealed class NativeErrorConverter : JsonConverter<string>
{
    private const int MaxDisplayLength = 1024;

    public override string? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        // System.Text.Json handles null reference values before calling this converter.
        if (reader.TokenType == JsonTokenType.String) return Bounded(reader.GetString()!);
        if (reader.TokenType != JsonTokenType.StartObject)
            throw new JsonException("Native error must be null, a string, or an object.");
        using var document = JsonDocument.ParseValue(ref reader);
        string? kind = null, detail = null;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var field in document.RootElement.EnumerateObject())
        {
            if (!seen.Add(field.Name)) throw new JsonException("Duplicate native error field.");
            if (field.Name is not ("kind" or "detail")) continue;
            if (field.Value.ValueKind is not (JsonValueKind.String or JsonValueKind.Null))
                throw new JsonException("Native error kind and detail must be strings or null.");
            if (field.Name == "kind") kind = field.Value.GetString();
            else detail = field.Value.GetString();
        }
        var label = kind switch
        {
            "decoder_unavailable" => "解码器不可用，请检查音频运行组件",
            "decode_failed" => "音频解码失败",
            _ => "音频服务报告了播放错误",
        };
        return Bounded(string.IsNullOrWhiteSpace(detail) ? label : label + "：" + detail.Trim());
    }

    private static string Bounded(string value) => value.Length <= MaxDisplayLength
        ? value : value[..(MaxDisplayLength - 1)] + "…";

    public override void Write(Utf8JsonWriter writer, string value, JsonSerializerOptions options) =>
        writer.WriteStringValue(value);
}
