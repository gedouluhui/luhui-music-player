// Test-only adapters for platform scheduling and HTTP. PlayerService and NativeState
// are compiled from their production sources. No WinUI window or audio device opens.
using System.Text.Json;

namespace Microsoft.UI.Dispatching
{
    public sealed class DispatcherQueue
    {
        public bool HasThreadAccess => true;
        public bool TryEnqueue(Action callback) { callback(); return true; }
    }
}
namespace Microsoft.UI.Xaml
{
    public sealed class DispatcherTimer
    {
        public TimeSpan Interval { get; set; }
        public bool IsEnabled { get; private set; }
        public event EventHandler<object>? Tick;
        public void Start() => IsEnabled = true;
        public void Stop() => IsEnabled = false;
        public void Fire() { if (IsEnabled) Tick?.Invoke(this, new object()); }
    }
}
namespace SaltPlayer.Native.Models
{
    public sealed class Track
    {
        public long Id { get; set; }
        public double? Duration { get; set; }
        public bool Favorite { get; set; }
        public bool IsPlaying { get; set; }
    }
    public sealed class SettingsDto
    {
        public double Volume { get; set; } = 0;
        public bool Muted { get; set; } = true;
        public string PlayMode { get; set; } = "normal";
        public bool ResumeOnStart { get; set; }
        public long? LastTrack { get; set; }
    }
    public sealed class QueueResponse
    {
        public List<Track> Queue { get; set; } = new();
        public int Index { get; set; }
        public string? Context { get; set; }
        public double Position { get; set; }
    }
    public sealed class OkResponse { public bool Ok { get; set; } }
    public sealed class FavoriteResponse { public bool Favorite { get; set; } }
}
namespace SaltPlayer.Native.Services
{
    public sealed class ApiClient
    {
        public readonly List<(string Method, string Path, string Payload)> Calls = new();
        public Func<string, string, string, Task<string>> Handler { get; set; } =
            (_, _, _) => Task.FromResult("{\"ok\":true}");
        public async Task<T> GetAsync<T>(string path, CancellationToken ct = default)
        {
            Calls.Add(("GET", path, ""));
            return JsonSerializer.Deserialize<T>(await Handler("GET", path, ""))!;
        }
        public async Task<T> PostAsync<T>(string path, object payload, CancellationToken ct = default)
        {
            var json = JsonSerializer.Serialize(payload);
            Calls.Add(("POST", path, json));
            return JsonSerializer.Deserialize<T>(await Handler("POST", path, json))!;
        }
        public Task<SaltPlayer.Native.Models.OkResponse> PostAsync(string path, object payload,
            CancellationToken ct = default) => PostAsync<SaltPlayer.Native.Models.OkResponse>(path, payload, ct);
    }
}
