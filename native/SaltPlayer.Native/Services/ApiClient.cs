using System.Net.Http;
using System.Text;
using System.Text.Json;
using SaltPlayer.Native.Models;

namespace SaltPlayer.Native.Services;

public sealed class ApiClient : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowReadingFromString,
        AllowTrailingCommas = true,
    };

    private readonly HttpClient _http;
    private string _token = "";

    public string BaseUrl { get; private set; } = "";
    public bool Ready => BaseUrl.Length > 0;
    public bool HasToken => _token.Length > 0;

    public ApiClient()
    {
        var handler = new SocketsHttpHandler
        {
            UseProxy = false,
            ConnectTimeout = TimeSpan.FromSeconds(4),
            PooledConnectionLifetime = TimeSpan.FromMinutes(10),
            AutomaticDecompression = System.Net.DecompressionMethods.None,
        };
        _http = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(18),
        };
    }

    public void Attach(string baseUrl, string? token = null)
    {
        BaseUrl = baseUrl.TrimEnd('/');
        _token = (token ?? "").Trim();
        _http.DefaultRequestHeaders.Remove("X-Salt-Token");
        if (_token.Length > 0)
        {
            _http.DefaultRequestHeaders.TryAddWithoutValidation("X-Salt-Token", _token);
        }
    }

    public async Task<T> GetAsync<T>(string path, CancellationToken ct = default)
    {
        using var res = await _http.GetAsync(BaseUrl + "/api/" + path, ct).ConfigureAwait(false);
        var body = await res.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
        {
            throw new HttpRequestException($"GET {path} → {(int)res.StatusCode} {Truncate(body)}");
        }
        return JsonSerializer.Deserialize<T>(body, JsonOptions)
               ?? throw new JsonException($"GET {path} 返回空对象");
    }

    public async Task<T> PostAsync<T>(string path, object payload, CancellationToken ct = default)
    {
        using var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
        using var res = await _http.PostAsync(BaseUrl + "/api/" + path, content, ct).ConfigureAwait(false);
        var body = await res.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
        {
            throw new HttpRequestException($"POST {path} → {(int)res.StatusCode} {Truncate(body)}");
        }
        return JsonSerializer.Deserialize<T>(body, JsonOptions)
               ?? throw new JsonException($"POST {path} 返回空对象");
    }

    public Task<OkResponse> PostAsync(string path, object payload, CancellationToken ct = default) =>
        PostAsync<OkResponse>(path, payload, ct);

    public async Task<byte[]?> GetBytesAsync(string pathAndQuery, CancellationToken ct = default)
    {
        using var res = await _http.GetAsync(BaseUrl + "/api/" + pathAndQuery, ct).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode) return null;
        return await res.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false);
    }

    // ImageSource / media elements cannot attach headers. The backend accepts ?token=
    // for the two read-only binary endpoints only, so the shell appends it here.
    private string TokenQuery => _token.Length == 0 ? "" : "&token=" + Uri.EscapeDataString(_token);

    public string TrackCover(long trackId, string size = "thumb") =>
        $"{BaseUrl}/api/cover?track_id={trackId}&size={Uri.EscapeDataString(size)}{TokenQuery}";

    public string TextCover(string album, string artist, string size = "art") =>
        $"{BaseUrl}/api/cover?album={Uri.EscapeDataString(album ?? "")}&artist={Uri.EscapeDataString(artist ?? "")}"
        + $"&size={Uri.EscapeDataString(size)}{TokenQuery}";

    public string AlbumCover(AlbumInfo album) =>
        album.CoverTrack is > 0 ? TrackCover(album.CoverTrack.Value, "art") : TextCover(album.Album, album.Artist ?? "");

    public string ArtistCover(ArtistInfo artist) =>
        artist.CoverTrack is > 0 ? TrackCover(artist.CoverTrack.Value, "art") : TextCover(artist.Name, artist.Name);

    public string PlaylistCover(PlaylistInfo playlist) =>
        playlist.CoverTrack is > 0 ? TrackCover(playlist.CoverTrack.Value, "art") : TextCover(playlist.Name, "");

    private static string Truncate(string s) => s.Length <= 300 ? s : s[..300] + "…";

    public void Dispose() => _http.Dispose();
}
