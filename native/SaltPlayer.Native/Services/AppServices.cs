using SaltPlayer.Native.Models;

namespace SaltPlayer.Native.Services;

public static class AppServices
{
    public static BackendService Backend { get; set; } = null!;
    public static ApiClient Api { get; set; } = null!;
    public static PlayerService Player { get; set; } = null!;
    public static BootstrapResponse Bootstrap { get; set; } = null!;
}
