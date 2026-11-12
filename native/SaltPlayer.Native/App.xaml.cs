using System.Security.Cryptography;
using System.Text;
using Microsoft.UI.Xaml;

namespace SaltPlayer.Native;

public partial class App : Application
{
    private Window? _window;
    private static Mutex? _instanceMutex;
    private static EventWaitHandle? _activateEvent;
    private static MainWindow? _mainWindow;
    private const string InstanceRoot = @"Local\SaltPlayer.Native.";

    public App()
    {
        InitializeComponent();
        // 启动期崩溃（0xc000027b）没有托管栈可看；审计时把未处理异常落到 trace 里定位。
        UnhandledException += (_, e) =>
        {
            var path = Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_TRACE");
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                var ex = e.Exception;
                File.AppendAllText(path,
                    $"{DateTime.Now:HH:mm:ss.fff} unhandled {ex?.GetType().Name}: {ex?.Message}\n{ex?.StackTrace}\n");
            }
            catch
            {
            }
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        if (!TryEnterSingleInstance())
        {
            Environment.Exit(0);
            return;
        }
        var main = new MainWindow();
        _mainWindow = main;
        _window = main;
        main.Activate();
    }

    // 双击第二次 = 已经有一份在跑：再起一份会开第二个后端抢同一个 SQLite/输出设备。
    // 用数据目录做键，审计（独立 SALTPLAYER_DATA 临时副本）与正式实例互不干扰。
    private static bool TryEnterSingleInstance()
    {
        try
        {
            var data = Environment.GetEnvironmentVariable("SALTPLAYER_DATA");
            if (string.IsNullOrWhiteSpace(data))
            {
                data = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "SaltPlayerPro");
            }
            var hash = Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(data).ToLowerInvariant())));
            var key = hash[..16];

            var mutex = new Mutex(initiallyOwned: true, InstanceRoot + "Mutex." + key, out var created);
            if (!created)
            {
                try
                {
                    if (EventWaitHandle.TryOpenExisting(InstanceRoot + "Activate." + key, out var existing))
                    {
                        existing.Set();
                        existing.Dispose();
                    }
                }
                catch
                {
                }
                return false;
            }
            _instanceMutex = mutex;
            _activateEvent = new EventWaitHandle(false, EventResetMode.AutoReset, InstanceRoot + "Activate." + key);
            var listener = new Thread(() =>
            {
                while (true)
                {
                    try
                    {
                        _activateEvent.WaitOne();
                        var window = _mainWindow;
                        window?.DispatcherQueue.TryEnqueue(window.ShowFromExternalLaunch);
                    }
                    catch
                    {
                    }
                }
            })
            {
                IsBackground = true,
                Name = "single-instance-activate",
            };
            listener.Start();
            return true;
        }
        catch
        {
            // 内核对象创建失败不该阻止启动；最坏退回旧的“允许双开”行为。
            return true;
        }
    }
}
