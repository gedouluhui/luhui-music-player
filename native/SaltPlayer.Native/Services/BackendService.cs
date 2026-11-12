using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;

namespace SaltPlayer.Native.Services;

/// <summary>
/// Owns the Python audio/library server. The native shell is pure UI; decoding, DSP,
/// WASAPI output, scanning and the SQLite library all stay in the already-tuned backend.
/// </summary>
public sealed class BackendService : IDisposable
{
    private readonly HttpClient _probe = new() { Timeout = TimeSpan.FromSeconds(2) };
    private Process? _process;
    private StreamWriter? _log;
    private readonly object _logLock = new();
    private bool _disposed;

    public string BaseUrl { get; private set; } = "";
    public bool Owned { get; private set; }
    public string? LastError { get; private set; }
    public string LogPath { get; } = Path.Combine(Path.GetTempPath(), "SaltPlayer.Native", "backend.log");
    public string? BackendRoot { get; private set; }
    public string? PythonPath { get; private set; }

    public async Task<bool> StartAsync(CancellationToken ct = default)
    {
        // Test hook: attach to a backend that is already running (no process ownership).
        var external = Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_API");
        if (!string.IsNullOrWhiteSpace(external))
        {
            BaseUrl = external.TrimEnd('/');
            Owned = false;
            return await WaitHealthyAsync(TimeSpan.FromSeconds(8), ct).ConfigureAwait(false);
        }

        BackendRoot = FindBackendRoot();
        if (BackendRoot is null)
        {
            LastError = "找不到后端目录（salt_player.py）";
            return false;
        }

        PythonPath = FindPython(BackendRoot);
        if (PythonPath is null)
        {
            LastError = "找不到 Python 运行时（可设置 SALTPLAYER_PYTHON）";
            return false;
        }

        Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);
        RotateLog();

        for (var attempt = 0; attempt < 3; attempt++)
        {
            ct.ThrowIfCancellationRequested();
            var port = PickFreePort();
            if (!Spawn(port))
            {
                continue;
            }
            BaseUrl = $"http://127.0.0.1:{port}";
            if (await WaitHealthyAsync(TimeSpan.FromSeconds(45), ct).ConfigureAwait(false))
            {
                Owned = true;
                return true;
            }
            StopProcess();
            if (LastError is not null && LastError.Contains("port", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            return false;
        }
        LastError ??= "后端启动失败";
        return false;
    }

    private bool Spawn(int port)
    {
        var script = Path.Combine(BackendRoot!, "salt_player.py");
        var psi = new ProcessStartInfo
        {
            FileName = PythonPath!,
            Arguments = $"\"{script}\" --server --port {port}",
            WorkingDirectory = BackendRoot!,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        psi.Environment["SALTPLAYER_NATIVE_SHELL"] = "1";

        try
        {
            lock (_logLock)
            {
                _log ??= new StreamWriter(new FileStream(LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite),
                                          new UTF8Encoding(false)) { AutoFlush = true };
                _log.WriteLine($"===== {DateTime.Now:O} spawn {PythonPath} {psi.Arguments} (cwd={BackendRoot}) =====");
            }
            var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
            p.OutputDataReceived += (_, e) => AppendLog("out", e.Data);
            p.ErrorDataReceived += (_, e) => AppendLog("err", e.Data);
            p.Exited += (_, _) => AppendLog("sys", $"backend exited code={SafeExitCode(p)}");
            if (!p.Start())
            {
                LastError = "后端进程未能启动";
                return false;
            }
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            _process = p;
            LastError = null;
            return true;
        }
        catch (Exception e)
        {
            LastError = "后端启动异常：" + e.Message;
            AppendLog("sys", LastError);
            return false;
        }
    }

    private async Task<bool> WaitHealthyAsync(TimeSpan timeout, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + timeout;
        Exception? last = null;
        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();
            if (_process is { HasExited: true })
            {
                LastError = $"后端进程已退出（code={SafeExitCode(_process)}），日志：{LogPath}";
                return false;
            }
            try
            {
                using var res = await _probe.GetAsync(BaseUrl + "/api/bootstrap", ct).ConfigureAwait(false);
                if (res.StatusCode == HttpStatusCode.OK)
                {
                    var text = await res.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                    if (text.Contains("\"ok\": true", StringComparison.Ordinal) ||
                        text.Contains("\"ok\":true", StringComparison.Ordinal))
                    {
                        return true;
                    }
                }
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                // probe timeout: server still importing scipy/numpy
            }
            catch (Exception e)
            {
                last = e;
            }
            await Task.Delay(160, ct).ConfigureAwait(false);
        }
        LastError = "等待后端响应超时" + (last is null ? "" : "：" + last.Message) + "，日志：" + LogPath;
        return false;
    }

    private void AppendLog(string channel, string? line)
    {
        if (line is null) return;
        try
        {
            lock (_logLock)
            {
                _log?.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] {channel} {line}");
            }
        }
        catch
        {
            // logging must never take the UI down
        }
    }

    private void RotateLog()
    {
        try
        {
            if (File.Exists(LogPath) && new FileInfo(LogPath).Length > 512 * 1024)
            {
                File.Move(LogPath, LogPath + ".1", overwrite: true);
            }
        }
        catch
        {
        }
    }

    private static int PickFreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string? FindBackendRoot()
    {
        var env = Environment.GetEnvironmentVariable("SALTPLAYER_BACKEND_DIR");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(Path.Combine(env, "salt_player.py")))
        {
            return Path.GetFullPath(env);
        }

        var packaged = Path.Combine(AppContext.BaseDirectory, "backend");
        if (File.Exists(Path.Combine(packaged, "salt_player.py")))
        {
            return packaged;
        }

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 12 && dir is not null; i++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "salt_player.py")) &&
                Directory.Exists(Path.Combine(dir.FullName, "sp")))
            {
                return dir.FullName;
            }
        }
        return null;
    }

    private static string? FindPython(string backendRoot)
    {
        var env = Environment.GetEnvironmentVariable("SALTPLAYER_PYTHON");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(env))
        {
            return env;
        }

        var local = new[]
        {
            Path.Combine(backendRoot, "python", "pythonw.exe"),
            Path.Combine(backendRoot, "runtime", "pythonw.exe"),
            Path.Combine(backendRoot, ".venv", "Scripts", "pythonw.exe"),
        };
        foreach (var p in local)
        {
            if (File.Exists(p)) return p;
        }

        var pathDirs = (Environment.GetEnvironmentVariable("PATH") ?? "")
            .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries);
        foreach (var name in new[] { "pythonw.exe", "python.exe" })
        {
            foreach (var d in pathDirs)
            {
                try
                {
                    var p = Path.Combine(d.Trim('"'), name);
                    if (File.Exists(p)) return p;
                }
                catch
                {
                }
            }
        }

        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var localApp = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var guessed = new List<string>();
        foreach (var ver in new[] { "313", "312", "311", "310" })
        {
            guessed.Add(Path.Combine(localApp, "Programs", "Python", $"Python{ver}", "pythonw.exe"));
            guessed.Add(Path.Combine(home, "anaconda3", "pythonw.exe"));
            guessed.Add(Path.Combine(home, "miniconda3", "pythonw.exe"));
        }
        foreach (var p in guessed)
        {
            if (File.Exists(p)) return p;
        }
        return null;
    }

    private static int? SafeExitCode(Process p)
    {
        try { return p.HasExited ? p.ExitCode : null; }
        catch { return null; }
    }

    private void StopProcess()
    {
        var p = _process;
        _process = null;
        if (p is null) return;
        try
        {
            if (!p.HasExited)
            {
                p.Kill(entireProcessTree: true);
                p.WaitForExit(3000);
            }
        }
        catch
        {
        }
        finally
        {
            p.Dispose();
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        if (Owned) StopProcess();
        _probe.Dispose();
        lock (_logLock)
        {
            try { _log?.Dispose(); } catch { }
            _log = null;
        }
    }
}
