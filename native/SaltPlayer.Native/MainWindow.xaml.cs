using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Microsoft.UI.Xaml.Media.Imaging;
using SaltPlayer.Native.Models;
using SaltPlayer.Native.Services;
using Windows.Foundation;
using Windows.Graphics;
using Windows.Graphics.Imaging;
using Windows.Storage.Streams;
using Windows.UI;
using Windows.UI.ViewManagement;

namespace SaltPlayer.Native;

public sealed partial class MainWindow : Window
{
    private const double WindowWidth = 1320;
    private const double WindowHeight = 830;

    private readonly Dictionary<long, Track> _trackCache = new();
    private readonly List<Track> _allTracks = new();
    private readonly List<Track> _recent = new();
    private readonly List<Track> _fresh = new();
    private readonly List<Track> _favorites = new();
    private readonly List<Track> _history = new();
    private readonly List<AlbumInfo> _albumsAll = new();
    private readonly List<ArtistInfo> _artistsAll = new();
    private readonly List<CategoryInfo> _categories = new();
    private List<Track> _view = new();
    private List<AlbumInfo> _homeAlbums = new();
    private List<Track> _base = new();
    private List<Track> _azTracks = new();
    private List<AzLetter> _azLetters = new();

    private readonly DispatcherTimer _searchDelay = new() { Interval = TimeSpan.FromMilliseconds(180) };
    private readonly DispatcherTimer _toastTimer = new() { Interval = TimeSpan.FromSeconds(2.4) };
    private readonly DispatcherTimer _azBubbleTimer = new() { Interval = TimeSpan.FromMilliseconds(760) };
    private readonly DispatcherTimer _scanPoll = new() { Interval = TimeSpan.FromMilliseconds(900) };
    private List<string> _excludes = new();
    private bool _scanBusy;

    private readonly Brush _accentBrush;
    private readonly Brush _dimBrush;
    private readonly Brush _accentSoftBrush;
    private readonly Brush _controlFillBrush;
    private readonly Brush _controlStrokeBrush;
    private readonly Brush _textPrimaryBrush;
    private readonly Brush _transparentBrush = new SolidColorBrush(Colors.Transparent);

    private BackendService? _backend;
    private ApiClient? _api;
    private PlayerService? _player;
    private BootstrapResponse? _boot;

    private string _activeNav = "home";
    private string _viewMode = "home";
    private string _sortMode = "default";
    private bool _sortDesc;
    private string _context = "";
    private string _subPrefix = "";
    private string _backTarget = "home";

    private int _favCount;
    private long _nowTrackId;
    private int _lastQueueCount = -1;
    private bool _ready;
    private bool _syncingSeek;
    private bool _seekDragging;
    private bool _syncingVolume;
    private bool _closing;
    private bool _closeAllowed;
    private bool _azMode;
    private bool _syncingSort;
    private ScrollViewer? _azScroll;
    private AzLetter? _azActive;

    private readonly List<LyricLine> _lyricLines = new();
    private ScrollViewer? _lyricScroll;
    private long _lyricTrackId;
    private int _lyricVersion;
    private int _lyricIndex = -1;
    private bool _lyricPlain;
    private bool _lyricOpen;
    private bool _showTranslation = true;
    private bool _playbarLyric = true;
    private double _lyricOffset;
    private double _lyricScale = 1;
    private DateTime _lyricHoldUntil;
    private string _nowSub = "";

    // 设置页：populate 期间抑制控件的保存回调，避免「打开页面就把默认值写回盘」。
    private bool _settingsPopulating;
    private SettingsDto _set = new();

    private TrayIcon? _tray;
    private MiniLyricsWindow? _mini;
    private MediaSession? _media;
    private UISettings? _uiSettings;
    private string _theme = "dark";
    private string _playbarLyricText = "";
    private RectInt32 _normalBounds;
    private bool _placementReady;
    private readonly List<Slider> _eqSliders = new();
    private readonly List<AudioDevice> _devices = new();
    private static readonly double[] EqFlat = new double[10];
    private static readonly Dictionary<string, double[]> EqPresets = new()
    {
        ["flat"] = new double[] { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 },
        ["rock"] = new double[] { 5, 4, 3, 1, -1, -1, 2, 4, 5, 5 },
        ["pop"] = new double[] { -2, 0, 2, 4, 5, 4, 2, 0, -1, -1 },
        ["jazz"] = new double[] { 3, 2, 1, 2, -1, -1, 1, 2, 3, 3 },
        ["classical"] = new double[] { 4, 3, 2, 0, 0, 0, 1, 3, 4, 4 },
        ["dance"] = new double[] { 6, 5, 2, 0, -1, 2, 5, 6, 5, 3 },
        ["vocal"] = new double[] { -2, -1, 0, 2, 5, 6, 5, 2, 0, -1 },
        ["bass"] = new double[] { 9, 8, 6, 3, 0, -1, -2, -2, -1, 0 },
        ["treble"] = new double[] { -2, -2, -1, 0, 1, 3, 5, 7, 8, 7 },
    };
    private static readonly string[] EqFreqs = { "31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k" };

    private readonly Stopwatch _auditClock = Stopwatch.StartNew();
    private readonly List<double> _auditFrames = new();
    private readonly List<double> _auditOffsets = new();
    private ScrollViewer? _auditScroll;
    private bool _auditScrolling;
    private double _auditLast;
    private string? _auditDir;

    private static readonly string? TracePath = Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_TRACE");

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    public MainWindow()
    {
        InitializeComponent();

        _accentBrush = (Brush)Application.Current.Resources["AccentBrush"];
        _dimBrush = (Brush)Application.Current.Resources["TextTertiaryBrush"];
        _accentSoftBrush = (Brush)Application.Current.Resources["AccentSoftBrush"];
        _controlFillBrush = (Brush)Application.Current.Resources["ControlFillBrush"];
        _controlStrokeBrush = (Brush)Application.Current.Resources["ControlStrokeBrush"];
        _textPrimaryBrush = (Brush)Application.Current.Resources["TextPrimaryBrush"];

        ConfigureWindow();
        _normalBounds = new RectInt32(AppWindow.Position.X, AppWindow.Position.Y,
            AppWindow.Size.Width, AppWindow.Size.Height);
        _placementReady = true;
        AppWindow.Changed += OnAppWindowChanged;
        InitTray();
        InitThemeWatcher();

        // Slider 默认的 Maximum 比 75 小，且解析器不保证先应用 Maximum；
        // 在 XAML 里写 Minimum="75" 会直接抛 XamlParseException，所以范围放代码里设。
        SetLyricSize.Maximum = 160;
        SetLyricSize.Minimum = 75;

        TrackList.ItemsSource = _view;
        AlbumGrid.ItemsSource = _albumsAll;
        ArtistGrid.ItemsSource = _artistsAll;
        CategoryGrid.ItemsSource = _categories;
        AzRail.ItemsSource = _azLetters;

        _searchDelay.Tick += (_, _) =>
        {
            _searchDelay.Stop();
            ApplyFilter();
        };
        _toastTimer.Tick += (_, _) => HideToast();
        _scanPoll.Tick += (_, _) => _ = PollScanAsync();
        _sleepTimer.Tick += SleepTimer_Tick;
        SleepMenu.MenuFlyoutPresenterStyle = GlassMenuPresenterStyle();
        _azBubbleTimer.Tick += (_, _) =>
        {
            _azBubbleTimer.Stop();
            AzBubble.Visibility = Visibility.Collapsed;
        };

        SeekSlider.AddHandler(UIElement.PointerPressedEvent, new PointerEventHandler(SeekSlider_PointerPressed), true);
        SeekSlider.AddHandler(UIElement.PointerReleasedEvent, new PointerEventHandler(SeekSlider_PointerReleased), true);
        SeekSlider.AddHandler(UIElement.PointerCanceledEvent, new PointerEventHandler(SeekSlider_PointerReleased), true);

        QueueFlyout.Opening += (_, _) =>
        {
            QueueList.ItemsSource = _player?.Queue;
            QueueCount.Text = _player is null ? "" : $"{_player.Queue.Count} 首";
            if (_player?.Current is { } current)
            {
                // 展开瞬间列表还没量出容器，直接 ScrollIntoView 找不到项；
                // 排到布局之后滚到当前曲目，长队列打开就能看到自己听到哪。
                DispatcherQueue.TryEnqueue(DispatcherQueuePriority.Low,
                    () => QueueList.ScrollIntoView(current, ScrollIntoViewAlignment.Leading));
            }
        };

        LyricsList.Loaded += (_, _) => _lyricScroll = FindScrollViewer(LyricsList);
        // 用户自己翻歌词时暂停自动跟随，停手 4 秒后恢复；否则每次换行都会把视口拽回去。
        PointerEventHandler lyricTouch = (_, _) => _lyricHoldUntil = DateTime.UtcNow.AddSeconds(4);
        LyricsList.AddHandler(UIElement.PointerWheelChangedEvent, lyricTouch, true);
        LyricsList.AddHandler(UIElement.PointerPressedEvent, lyricTouch, true);

        RootGrid.Loaded += (_, _) => _ = BootAsync();
        AppWindow.Closing += OnAppWindowClosing;
        Closed += OnClosed;
    }

    private void HeroCard_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        // 窄窗口下让出统计列，否则两个操作按钮没有立足之地。
        HeroStats.Visibility = e.NewSize.Width < 700 ? Visibility.Collapsed : Visibility.Visible;
    }

    private void RootGrid_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        // 背景光晕带负 Margin 溢出到窗口外；裁掉以后 RenderTargetBitmap 也不会把
        // 溢出的几百像素一起截进来（审计截图四周因此不再出现大块空白）。
        RootGrid.Clip = new RectangleGeometry
        {
            Rect = new Rect(0, 0, e.NewSize.Width, e.NewSize.Height),
        };
        UpdateAdaptiveLayout(e.NewSize.Width);
    }

    private bool? _narrowHeader;
    private bool? _playerCompact;
    private string _qualityFull = "";
    private string _qualityShort = "";

    private void UpdateAdaptiveLayout(double windowWidth)
    {
        // 内容区 = 窗口宽 - ShellHost 左右 10 - 侧栏 268。
        var contentWidth = Math.Max(0, windowWidth - 288);
        var narrow = contentWidth < 880;
        if (_narrowHeader != narrow)
        {
            _narrowHeader = narrow;
            if (narrow)
            {
                // 标题与工具同排时在 960 DIP 必然交叠：窄窗让工具独占第二行。
                Grid.SetRow(HeaderActions, 1);
                HeaderActions.HorizontalAlignment = HorizontalAlignment.Left;
                HeaderActions.Margin = new Thickness(0, 8, 0, 0);
                SearchHost.Width = 220;
            }
            else
            {
                Grid.SetRow(HeaderActions, 0);
                HeaderActions.HorizontalAlignment = HorizontalAlignment.Right;
                HeaderActions.Margin = new Thickness(0);
                SearchHost.Width = 250;
            }
            Trace($"layout header narrow={narrow} content={contentWidth:0}");
        }
        ApplyPlayerBarDensity(windowWidth);
        UpdateLyricsHeaderLayout(contentWidth);
    }

    private void ApplyPlayerBarDensity(double windowWidth)
    {
        var compact = windowWidth < 1200;
        if (_playerCompact == compact) return;
        _playerCompact = compact;
        VolumeSlider.Width = compact ? 64 : 86;
        QualityBadge.Text = compact ? _qualityShort : _qualityFull;
        Trace($"player density compact={compact}");
    }

    private void SidebarScroller_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        // ScrollableHeight 要等 ScrollViewer 完成 arrange 才准确，排到布局之后读取。
        DispatcherQueue.TryEnqueue(UpdateSidebarFade);
    }

    private void SidebarScroller_ViewChanged(object? sender, ScrollViewerViewChangedEventArgs e)
    {
        UpdateSidebarFade();
    }

    private void UpdateSidebarFade()
    {
        // 此 SDK 的 UIElement 没有 OpacityMask；改为按 item 到底边的距离逐个淡出，
        // 在玻璃面板上是真透明，不依赖底色匹配。滚到底时不淡出，最后一项完整可读。
        var scrollable = SidebarScroller.ScrollableHeight;
        var offset = SidebarScroller.VerticalOffset;
        var viewport = SidebarScroller.ViewportHeight;
        var fade = scrollable > 0 && viewport > 0 && scrollable - offset > 1;
        const double zone = 40;
        var edge = offset + viewport;
        foreach (var child in SidebarContent.Children)
        {
            if (child is not FrameworkElement el) continue;
            if (!fade)
            {
                el.Opacity = 1;
                continue;
            }
            var y = el.TransformToVisual(SidebarContent).TransformPoint(new Point(0, 0)).Y;
            // 用中心点算透明度：折线处的行半隐半现，才是「下面还有」的提示；
            // 按底边算会让整行直接归零，留下一块空白。
            var center = y + el.ActualHeight / 2;
            el.Opacity = center <= edge - zone ? 1 : Math.Clamp((edge - center) / zone, 0, 1);
        }
    }

    private void UpdateLyricsHeaderLayout(double contentWidth)
    {
        var compact = contentWidth < 980;
        var text = compact ? Visibility.Collapsed : Visibility.Visible;
        LyricsAttachText.Visibility = text;
        LyricsEditText.Visibility = text;
        LyricsTransText.Visibility = text;
        var pad = new Thickness(compact ? 9 : 12, 0, compact ? 9 : 12, 0);
        LyricsAttachButton.Padding = pad;
        LyricsEditButton.Padding = pad;
        LyricsTransButton.Padding = pad;
        LyricsTitle.MaxWidth = Math.Clamp(contentWidth - 400, 140, 420);
    }

    private void ConfigureWindow()
    {
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleDrag);

        ApplyTitleBarColors(false);

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var scale = GetDpiForWindow(hwnd) / 96.0;
        if (scale <= 0 || double.IsNaN(scale)) scale = 1.0;

        var width = (int)Math.Round(WindowWidth * scale);
        var height = (int)Math.Round(WindowHeight * scale);
        AppWindow.ResizeClient(new SizeInt32(width, height));

        // 布局的最小可用宽度是 960 DIP（侧栏 268 + 内容 672），再窄页头与播放条就无处可退。
        if (AppWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.PreferredMinimumWidth = (int)Math.Round(960 * scale);
            presenter.PreferredMinimumHeight = (int)Math.Round(620 * scale);
        }

        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
        if (area is not null)
        {
            var x = area.WorkArea.X + (area.WorkArea.Width - width) / 2;
            var y = area.WorkArea.Y + (area.WorkArea.Height - height) / 2;
            AppWindow.Move(new PointInt32(x, y));
        }

        try
        {
            var icon = Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico");
            if (File.Exists(icon)) AppWindow.SetIcon(icon);
        }
        catch
        {
        }
    }

    // ------------------------------------------------------- 主题 / 托盘 / 窗口

    private void InitTray()
    {
        _tray = new TrayIcon
        {
            ShowRequested = RestoreFromTray,
            PlayPauseRequested = () => _ = _player?.ToggleAsync(),
            PreviousRequested = () => _ = _player?.PreviousAsync(),
            NextRequested = () => _ = _player?.NextAsync(),
            ExitRequested = ExitFromTray,
        };
        Trace("tray added=" + _tray.IsAdded);
    }

    private void InitThemeWatcher()
    {
        try
        {
            _uiSettings = new UISettings();
            _uiSettings.ColorValuesChanged += (_, _) =>
            {
                if (_theme != "system") return;
                // 系统主题事件在别的线程来，必须回 UI 线程改画刷。
                DispatcherQueue.TryEnqueue(() =>
                {
                    if (_theme == "system") ApplyTheme("system");
                });
            };
        }
        catch
        {
            _uiSettings = null;
        }
    }

    private void ApplyTheme(string theme)
    {
        _theme = theme is "light" or "system" ? theme : "dark";
        var light = _theme == "light" || (_theme == "system" && SystemPrefersLight());
        RootGrid.RequestedTheme = light ? ElementTheme.Light : ElementTheme.Dark;
        ThemePalette.Apply(light);
        NoiseLayer.Opacity = light ? 0.12 : 0.32;
        ApplyTitleBarColors(light);
        _mini?.ApplyTheme(light);
        if (ComboTag(SetTheme) != _theme)
        {
            var guard = _settingsPopulating;
            _settingsPopulating = true;
            SelectByTag(SetTheme, _theme);
            _settingsPopulating = guard;
        }
        Trace($"theme {_theme} light={light}");
    }

    private static bool SystemPrefersLight()
    {
        try
        {
            var value = Microsoft.Win32.Registry.GetValue(
                @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                "AppsUseLightTheme", 0);
            return value is int v && v != 0;
        }
        catch
        {
            return false;
        }
    }

    private void ApplyTitleBarColors(bool light)
    {
        var bar = AppWindow.TitleBar;
        bar.ButtonBackgroundColor = Colors.Transparent;
        bar.ButtonInactiveBackgroundColor = Colors.Transparent;
        bar.ButtonHoverBackgroundColor = light
            ? Color.FromArgb(0x16, 0x00, 0x00, 0x00)
            : Color.FromArgb(0x1C, 0xFF, 0xFF, 0xFF);
        bar.ButtonPressedBackgroundColor = light
            ? Color.FromArgb(0x26, 0x00, 0x00, 0x00)
            : Color.FromArgb(0x30, 0xFF, 0xFF, 0xFF);
        bar.ButtonForegroundColor = light
            ? Color.FromArgb(0xFF, 0x56, 0x5D, 0x6B)
            : Color.FromArgb(0xFF, 0xA7, 0xAE, 0xBC);
        bar.ButtonInactiveForegroundColor = light
            ? Color.FromArgb(0xFF, 0x9A, 0xA1, 0xAE)
            : Color.FromArgb(0xFF, 0x5C, 0x64, 0x72);
        bar.ButtonHoverForegroundColor = light
            ? Color.FromArgb(0xFF, 0x19, 0x1C, 0x22)
            : Color.FromArgb(0xFF, 0xF4, 0xF5, 0xF8);
    }

    private void OnAppWindowChanged(AppWindow sender, AppWindowChangedEventArgs args)
    {
        if (!_placementReady) return;
        if (!args.DidSizeChange && !args.DidPositionChange) return;
        if (sender.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Restored })
            _normalBounds = new RectInt32(sender.Position.X, sender.Position.Y, sender.Size.Width, sender.Size.Height);
    }

    private double WindowScale()
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var scale = GetDpiForWindow(hwnd) / 96.0;
        return scale > 0 && !double.IsNaN(scale) ? scale : 1.0;
    }

    private void ApplyPlacement(WindowPlacementDto placement)
    {
        try
        {
            // settings 里存 DIP（和 Qt 版 / XAML 一致），MoveAndResize 要物理像素。
            // 之前直接按物理像素套 1320 宽，在 150% 缩放下只剩 880 DIP，英雄卡被挤爆。
            var scale = WindowScale();
            var hasPos = placement.X is int && placement.Y is int;
            var area = hasPos
                ? DisplayArea.GetFromPoint(
                    new PointInt32((int)Math.Round(placement.X!.Value * scale),
                                   (int)Math.Round(placement.Y!.Value * scale)),
                    DisplayAreaFallback.Nearest)
                : DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
            area ??= DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
            var work = area.WorkArea;

            var workWDip = work.Width / scale;
            var workHDip = work.Height / scale;
            var wDip = Math.Clamp(placement.W > 0 ? placement.W : WindowWidth, 960, Math.Max(960, workWDip));
            var hDip = Math.Clamp(placement.H > 0 ? placement.H : WindowHeight, 620, Math.Max(620, workHDip));
            var w = (int)Math.Round(wDip * scale);
            var h = (int)Math.Round(hDip * scale);

            int x, y;
            if (hasPos)
            {
                x = (int)Math.Round(placement.X!.Value * scale);
                y = (int)Math.Round(placement.Y!.Value * scale);
                // 上次可能是在别的分辨率/缩放上存的，整窗收回工作区，避免只露一条边。
                x = Math.Clamp(x, work.X, work.X + work.Width - w);
                y = Math.Clamp(y, work.Y, work.Y + work.Height - h);
            }
            else
            {
                x = work.X + (work.Width - w) / 2;
                y = work.Y + (work.Height - h) / 2;
            }

            AppWindow.MoveAndResize(new RectInt32(x, y, w, h));
            _normalBounds = new RectInt32(x, y, w, h);
            if (placement.Maximized && AppWindow.Presenter is OverlappedPresenter presenter)
                presenter.Maximize();
            Trace($"placement {wDip:0}x{hDip:0}dip scale={scale:0.##} at {x},{y} max={placement.Maximized}");
        }
        catch (Exception e)
        {
            Trace("placement-error " + e.Message);
        }
    }

    private async Task SaveWindowPlacementAsync()
    {
        if (_api is null || !_placementReady || _normalBounds.Width <= 0) return;
        var maximized = AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Maximized };
        var bounds = _normalBounds;
        var scale = WindowScale();
        await SaveSettingsAsync(new Dictionary<string, object>
        {
            ["window"] = new
            {
                w = (int)Math.Round(bounds.Width / scale),
                h = (int)Math.Round(bounds.Height / scale),
                x = (int?)Math.Round(bounds.X / scale),
                y = (int?)Math.Round(bounds.Y / scale),
                maximized,
            },
        });
    }

    // ------------------------------------------------------- 桌面歌词

    private void ShowMiniLyrics(bool persist)
    {
        if (_mini is null)
        {
            var light = _theme == "light" || (_theme == "system" && SystemPrefersLight());
            var mw = _set.MiniWindow;
            _mini = new MiniLyricsWindow(_set.DesktopLyricScale, _set.DesktopLyricOnTop, light,
                mw.X, mw.Y, mw.W, mw.H);
            _mini.CloseRequested += () => HideMiniLyrics(persist: true);
            _mini.TopChanged += on =>
            {
                _set.DesktopLyricOnTop = on;
                SetDesktopLyricTop.IsOn = on;
                QueueSave("desktop_lyric_on_top", on);
            };
            _mini.ScaleChanged += scale =>
            {
                _set.DesktopLyricScale = scale;
                QueueSave("desktop_lyric_scale", scale);
            };
            _mini.Moved += rect =>
            {
                _set.MiniWindow = new MiniWindowDto { X = rect.X, Y = rect.Y, W = rect.Width, H = rect.Height };
                QueueSave("mini_window", new { x = rect.X, y = rect.Y, w = rect.Width, h = rect.Height });
            };
            _mini.Closed += (_, _) => _mini = null;
            _mini.SetTrack(_player?.Current?.TitleText, _player?.Current?.ArtistText, _player?.IsPlaying == true);
            _mini.SetLyric(_playbarLyricText);
        }
        _mini.ApplyTheme(_theme == "light" || (_theme == "system" && SystemPrefersLight()));
        _mini.ShowNoActivate();
        _set.DesktopLyric = true;
        if (persist) QueueSave("desktop_lyric", true);
        SetDesktopLyric.IsOn = true;
        SetDesktopLyricTop.IsOn = _set.DesktopLyricOnTop;
        SetDesktopLyricTop.IsEnabled = true;
        try
        {
            var size = _mini.AppWindow.Size;
            var pos = _mini.AppWindow.Position;
            var top = (_mini.AppWindow.Presenter as OverlappedPresenter)?.IsAlwaysOnTop;
            Trace($"mini shown {size.Width}x{size.Height} at {pos.X},{pos.Y} top={top}");
        }
        catch { Trace("mini shown"); }
    }

    private void HideMiniLyrics(bool persist)
    {
        _mini?.Close();
        _mini = null;
        _set.DesktopLyric = false;
        if (persist) QueueSave("desktop_lyric", false);
        SetDesktopLyric.IsOn = false;
        SetDesktopLyricTop.IsEnabled = false;
        Trace("mini hidden");
    }

    private void RestoreFromTray()
    {
        try
        {
            AppWindow.Show();
            if (AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized } presenter)
                presenter.Restore();
            Activate();
        }
        catch (Exception e)
        {
            Trace("tray-show-error " + e.Message);
        }
    }

    // 第二次启动 exe 时由 App 的单实例监听线程转发：把已隐藏/最小化的主窗拉回前台。
    internal void ShowFromExternalLaunch() => RestoreFromTray();

    private void HideToTray()
    {
        _ = SaveWindowPlacementAsync();
        AppWindow.Hide();
        Trace("close-to-tray");
    }

    private void ExitFromTray()
    {
        if (_closing) return;
        _closing = true;
        _ = ShutdownAsync();
    }

    // ------------------------------------------------------------------ boot

    private static void Trace(string message)
    {
        if (string.IsNullOrEmpty(TracePath)) return;
        try
        {
            File.AppendAllText(TracePath, $"{DateTime.Now:HH:mm:ss.fff} {message}{Environment.NewLine}");
        }
        catch
        {
        }
    }

    private async Task BootAsync()
    {
        if (_ready) return;
        Trace("boot");
        try
        {
            SetBootStatus("正在启动音频服务…");
            _backend = new BackendService();
            AppServices.Backend = _backend;
            if (!await _backend.StartAsync())
            {
                ShowBootError(_backend.LastError ?? "后端启动失败");
                return;
            }

            _api = new ApiClient();
            _api.Attach(_backend.BaseUrl);
            AppServices.Api = _api;
            Trace("backend " + _backend.BaseUrl);

            SetBootStatus("正在读取曲库…");
            _boot = await _api.GetAsync<BootstrapResponse>("bootstrap");
            Trace($"bootstrap tracks={_boot.Stats.Tracks} albums={_boot.Stats.Albums} playlists={_boot.Playlists.Count}");
            AppServices.Bootstrap = _boot;

            _set = _boot.Settings;
            ApplyPlacement(_set.Window);
            ApplyTheme(_set.Theme);

            _player = new PlayerService(_api, DispatcherQueue);
            AppServices.Player = _player;
            _player.Changed += SyncPlayerUi;
            _player.ErrorOccurred += ShowToast;
            _player.ApplySettings(_boot.Settings);
            ApplyLyricSettings(_boot.Settings);

            ApplyBootstrap(_boot);
            await LoadLibraryAsync();

            SetBootStatus("正在恢复播放队列…");
            await _player.RestoreAsync(_boot.Settings);
            _player.Rebind(Unify);
            SyncPlayerUi();
            try
            {
                _media = new MediaSession(
                    WinRT.Interop.WindowNative.GetWindowHandle(this),
                    DispatcherQueue,
                    _player,
                    id => _api!.TrackCover(id, "art"));
                Trace("smtc ready");
            }
            catch (Exception ex)
            {
                Trace("smtc-error " + ex.Message);
            }
            if (_set.DesktopLyric) ShowMiniLyrics(persist: false);

            Trace($"library tracks={_allTracks.Count} albums={_albumsAll.Count} artists={_artistsAll.Count} recent={_recent.Count}");
            _ready = true;
            HideBoot();
            await GoToAsync("home");
            Trace("ready");

            if (Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_SCENARIO") == "autoskip")
            {
                await AutoskipScenarioAsync();
            }

            if (Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_SHOT") is { Length: > 0 } shot)
            {
                await Task.Delay(900);
                try
                {
                    await CaptureAsync(shot);
                    Trace("shot " + shot);
                }
                catch (Exception ex)
                {
                    Trace("shot-error " + ex.Message);
                }
            }

            if (Environment.GetEnvironmentVariable("SALTPLAYER_NATIVE_AUDIT") is { Length: > 0 } auditDir)
            {
                await AuditScenarioAsync(auditDir);
            }
        }
        catch (Exception e)
        {
            Trace("error " + e);
            ShowBootError(e.Message);
        }
    }

    private async Task CaptureAsync(string path, FrameworkElement? element = null)
    {
        var bitmap = new RenderTargetBitmap();
        await bitmap.RenderAsync(element ?? RootGrid);
        var pixels = await bitmap.GetPixelsAsync();
        using var stream = new InMemoryRandomAccessStream();
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
            (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight, 96, 96, pixels.ToArray());
        await encoder.FlushAsync();
        stream.Seek(0);
        using var file = File.Create(path);
        await stream.AsStreamForRead().CopyToAsync(file);
    }

    // ----------------------------------------------------------- audit driver

    // 复现「自动连跳」用：SALTPLAYER_NATIVE_SCENARIO=autoskip 时在 ready 之后重放用户那一串
    // （恢复播放 → 切歌 → 上一首 → 切回 → 进度条到曲尾 → 自然结束 → 自动下一首），
    // 全程走真实 PlayerService；判定全在迹线里（前端 [svc] + 引擎 [eng-trace]）。
    private async Task AutoskipScenarioAsync()
    {
        var t0 = DateTime.Now;
        string Rel() => (DateTime.Now - t0).TotalSeconds.ToString("0.00");
        var p = _player;
        if (p is null)
        {
            Trace("autoskip no-player");
            return;
        }
        long lastId = -1;
        bool? lastPlaying = null;
        Trace($"autoskip begin cur={p.Current?.Id ?? -1} idx={p.Index} " +
              $"pos={p.Position:0.00}/{p.Duration:0.00} playing={p.IsPlaying} mode={p.PlayMode}");
        for (var i = 0; i < 75; i++)   // 15 s
        {
            await Task.Delay(200);
            var id = p.Current?.Id ?? -1;
            var playing = p.IsPlaying;
            if (id == lastId && playing == lastPlaying) continue;
            Trace($"autoskip t={Rel()} cur={id} idx={p.Index} playing={playing} " +
                  $"pos={p.Position:0.00}/{p.Duration:0.00} busy={p.IsBusy}");
            lastId = id;
            lastPlaying = playing;
        }
        Trace($"autoskip watch-end t={Rel()} cur={p.Current?.Id ?? -1} idx={p.Index}");
        var yuai = _allTracks.FirstOrDefault(t => t.Id == 1679);
        if (yuai is not null)
        {
            var idx = _allTracks.IndexOf(yuai);
            Trace($"autoskip manual-play 雨爱 idx={idx}");
            await p.PlayFromAsync(_allTracks, idx, "所有歌曲");
            await Task.Delay(2000);
            Trace($"autoskip manual-next from={p.Index}");
            await p.NextAsync();
            await Task.Delay(1500);
            Trace($"autoskip manual-prev from={p.Index}");
            await p.PreviousAsync();
            await Task.Delay(1200);
            Trace($"autoskip manual-back 雨爱 idx={idx}");
            await p.PlayFromAsync(_allTracks, idx, "所有歌曲");
            await Task.Delay(2000);
            Trace($"autoskip manual-seek to={Math.Max(0, p.Duration - 2):0.00}/{p.Duration:0.00}");
            await p.SeekAsync(Math.Max(0, p.Duration - 2));
            await Task.Delay(10000);
        }
        Trace($"autoskip done t={Rel()} cur={p.Current?.Id ?? -1} playing={p.IsPlaying} " +
              $"pos={p.Position:0.00}/{p.Duration:0.00} err={p.LastError ?? "-"}");
    }

    private async Task AuditScenarioAsync(string dir)
    {
        _auditDir = dir;
        Directory.CreateDirectory(dir);
        CompositionTarget.Rendering += AuditRendering;
        try
        {
            Trace("audit begin");
            var bootTheme = ComboTag(SetTheme);
            // 闲置窗口：runner 在这 8 秒里采样 CPU/内存，任何 UI 工作都会污染读数。
            Trace("audit idle-begin");
            await Task.Delay(8000);
            Trace("audit idle-end");
            await ShotAsync("01-home.png");

            await GoToAsync("songs");
            await Task.Delay(700);
            Trace($"audit layout root={RootGrid.ActualWidth:0.0} shell={ShellHost.ActualWidth:0.0} " +
                  $"content={ContentHost.ActualWidth:0.0} header={HeaderHost.ActualWidth:0.0} " +
                  $"title={TitleGroup.ActualWidth:0.0}/{TitleGroup.DesiredSize.Width:0.0} " +
                  $"actions={HeaderActions.ActualWidth:0.0}/{HeaderActions.DesiredSize.Width:0.0} " +
                  "kids=" + string.Join(",",
                      HeaderActions.Children.OfType<FrameworkElement>()
                          .Select(c => $"{c.GetType().Name}:{c.ActualWidth:0.0}/{c.DesiredSize.Width:0.0}")));
            await ShotAsync("02-songs-top.png");

            // A–Z 字母轨：开启、跳一个字母、回到默认排序
            try
            {
                if (_azTracks.Count == 0) await LoadAzDataAsync();
                _azMode = true;
                ShowSongs();
                await Task.Delay(700);
                await ShotAsync("02b-songs-az.png");
                Trace($"audit az-active={_azActive?.Letter ?? "-"} rail={AzRailHost.Visibility}");
                var jump = _azLetters.LastOrDefault(l => l.Count > 0 && l.Letter is "Z" or "W" or "S" or "J")
                           ?? _azLetters.LastOrDefault(l => l.Count > 0);
                if (jump is not null)
                {
                    SetAzActive(jump);
                    ShowAzBubble(jump.Letter);
                    TrackList.ScrollIntoView(_view[jump.Offset], ScrollIntoViewAlignment.Leading);
                    await Task.Delay(700);
                    Trace($"audit az jump {jump.Letter} offset={jump.Offset} " +
                          $"top={FindScrollViewer(TrackList)?.VerticalOffset:0}");
                    await ShotAsync("02c-songs-az-jump.png");
                }
                _azMode = false;
                ShowSongs();
                await Task.Delay(500);
            }
            catch (Exception ex)
            {
                Trace("audit az-error " + ex.Message);
                _azMode = false;
            }

            var sv = FindScrollViewer(TrackList);
            if (sv is not null)
            {
                _auditScroll = sv;
                Trace($"audit scrollable={sv.ScrollableHeight:0} viewport={sv.ViewportHeight:0} " +
                      $"items={_view.Count}");
                await ScrollStepsAsync(sv, 0, 120, 2400, "wheel-like");
                await Task.Delay(300);
                await ShotAsync("03-songs-mid.png");

                sv.ChangeView(null, 0, null, true);
                await Task.Delay(400);
                await FlingAsync(sv, 24000, 2400, "single-fling");
                await Task.Delay(300);
                await ShotAsync("04-songs-fling.png");
                _auditScroll = null;
            }

            SearchBox.Text = "周";
            await Task.Delay(700);
            Trace($"audit search 周 -> {_view.Count}/{_base.Count}");
            await ShotAsync("05-search.png");
            SearchBox.Text = "";
            await Task.Delay(450);

            await GoToAsync("albums");
            await Task.Delay(800);
            await ShotAsync("06-albums.png");

            await GoToAsync("artists");
            await Task.Delay(800);
            await ShotAsync("07-artists.png");
            var artist = _artistsAll.FirstOrDefault(a => a.Name == "周杰伦") ?? _artistsAll.FirstOrDefault();
            if (artist is not null)
            {
                await ShowArtistAsync(artist);
                await Task.Delay(700);
                Trace($"audit artist-hero h={DetailHero.ActualHeight:0} \"{DetailTitle.Text}\" " +
                      $"round={DetailArtBorder.CornerRadius.TopLeft:0}");
                await ShotAsync("07b-artist.png");
            }

            if (_albumsAll.Count > 0)
            {
                var sample = _albumsAll.FirstOrDefault(a => a.Tracks > 3 && a.Album != "单曲")
                             ?? _albumsAll[0];
                await ShowAlbumAsync(sample);
                await Task.Delay(600);
                Trace($"audit album-hero h={DetailHero.ActualHeight:0} \"{DetailTitle.Text}\" " +
                      $"art={DetailArt.ActualWidth:0}x{DetailArt.ActualHeight:0}");
                await ShotAsync("08-album.png");
            }

            await GoToAsync("folders");
            await Task.Delay(800);
            Trace($"audit folders={_categories.Count}");
            await ShotAsync("08b-folders.png");
            if (_categories.Count > 0)
            {
                await ShowCategoryTracksAsync(_categories[0]);
                await Task.Delay(700);
                Trace($"audit folder-tracks={_view.Count}");
                await ShotAsync("08c-folder-tracks.png");
            }

            await GoToAsync("genres");
            await Task.Delay(700);
            Trace($"audit genres={_categories.Count}");
            await ShotAsync("08d-genres.png");

            await GoToAsync("songs");
            await Task.Delay(600);
            if (Environment.GetEnvironmentVariable("SALTPLAYER_SILENCE") == "1" &&
                _player is not null && _view.Count > 0)
            {
                await _player.PlayFromAsync(_view, Math.Min(4, _view.Count - 1), _context);
                await Task.Delay(1200);
                var row = _player.Current is null ? -1 : _view.FindIndex(t => t.Id == _player.Current.Id);
                if (row >= 0)
                {
                    TrackList.ScrollIntoView(_view[row], ScrollIntoViewAlignment.Leading);
                    await Task.Delay(700);
                }
                Trace($"audit play \"{_player.Current?.TitleText}\" playing={_player.IsPlaying} " +
                      $"row={row} hero={DetailHero.Visibility}");
                await ShotAsync("09-playing.png");
                Trace("audit smtc " + (_media?.DebugState() ?? "none"));

                // 歌词面板：挑一首词足够多的曲目（探测 API，避开只有几行的测试夹具），
                // 跳到中段让当前行高亮，并验证播放条歌词。
                var lyricRow = -1;
                foreach (var cand in _view.Where(t => t.HasLyric == 1).Take(40))
                {
                    try
                    {
                        var probe = await _api!.GetAsync<LyricsResponse>($"lyrics?track_id={cand.Id}");
                        if (probe.Ok && !probe.Plain && probe.Lines.Count >= 8)
                        {
                            lyricRow = _view.IndexOf(cand);
                            break;
                        }
                    }
                    catch { }
                }
                if (lyricRow < 0)
                {
                    var fallback = _view.FirstOrDefault(t => t.HasLyric == 1);
                    lyricRow = fallback is null ? -1 : _view.IndexOf(fallback);
                }
                if (lyricRow >= 0)
                {
                    await _player.PlayFromAsync(_view, lyricRow, _context);
                    await Task.Delay(1500);
                    OpenLyrics();
                    await Task.Delay(1500);
                    if (_lyricLines.Count >= 2 && !_lyricPlain)
                    {
                        var jump = _lyricLines[Math.Min(_lyricLines.Count / 2, _lyricLines.Count - 1)];
                        await _player.SeekAsync(Math.Max(0, jump.T + _lyricOffset + 0.4));
                        await Task.Delay(1300);
                    }
                    // 播放条歌词默认可能被用户关掉；审计里临时打开验证这条路径（不落盘）。
                    var savedPlaybar = _playbarLyric;
                    _playbarLyric = true;
                    // SyncLyric 在行号不变时不会刷播放条，这里强制走一遍 SetLyricIndex。
                    SetLyricIndex(_lyricIndex, scroll: false);
                    Trace($"audit lyrics open={_lyricOpen} lines={_lyricLines.Count} idx={_lyricIndex} " +
                          $"plain={_lyricPlain} offset={_lyricOffset:0.0} pos={_player.Position:0.0} " +
                          $"track=\"{_player.Current?.TitleText}\" playbar=\"{NowArtist.Text}\"");
                    await ShotAsync("09b-lyrics.png");
                    _playbarLyric = savedPlaybar;
                    SetLyricIndex(_lyricIndex, scroll: false);
                    CloseLyrics();
                    await Task.Delay(400);
                }
                else
                {
                    Trace("audit lyrics skipped: no track with lyric");
                }
            }

            // 设置页：渲染、控件填充、EQ 预设落盘与 configure 路径。
            // 不碰独占（会锁声卡）、不点「添加文件夹」（会弹前台 dialog）。
            try
            {
                await GoToAsync("settings");
                await Task.Delay(900);
                var panePos = SettingsColLeft.TransformToVisual(RootGrid).TransformPoint(new Point(0, 0));
                var svGeom = FindScrollViewer(SettingsView);
                Trace($"audit settings geom pane={SettingsColLeft.ActualWidth:0}x{SettingsColLeft.ActualHeight:0} " +
                      $"at={panePos.X:0},{panePos.Y:0} card={SettingsPlayCard.ActualWidth:0} " +
                      $"wide={_settingsWide} columns={SettingsColumns.ActualWidth:0} " +
                      $"extent={svGeom?.ExtentWidth:0}x{svGeom?.ExtentHeight:0} " +
                      $"extent={svGeom?.ExtentWidth:0}x{svGeom?.ExtentHeight:0} " +
                      $"viewport={svGeom?.ViewportWidth:0}x{svGeom?.ViewportHeight:0} " +
                      $"hoff={svGeom?.HorizontalOffset:0}");
                Trace($"audit settings loaded rate={_set.Rate:0.##} mode=\"{_set.PlayMode}\" " +
                      $"devices={SetDevice.Items.Count}/{_devices.Count} target={SetTargetRate.Items.Count} " +
                      $"eq={_set.EqEnabled} preset=\"{_set.EqPreset}\" gains={_set.EqGains?.Length ?? 0} " +
                      $"sliders={_eqSliders.Count} libs={_boot?.Libraries.Count ?? 0} " +
                      $"about=\"{AboutVersion.Text}\"");
                await ShotAsync("11-settings.png");

                var presetBefore = ComboTag(SetEqPreset);
                var presetTarget = presetBefore == "rock" ? "pop" : "rock";
                SelectByTag(SetEqPreset, presetTarget);
                await Task.Delay(1100);   // 350ms 防抖 + 落盘 + configure
                var saved = await _api!.GetAsync<SettingsResponse>("settings");
                var sg = saved.Settings.EqGains;
                Trace($"audit settings preset \"{presetBefore}\"->\"{ComboTag(SetEqPreset)}\" " +
                      $"saved=\"{saved.Settings.EqPreset}\" " +
                      $"gain0={(sg is { Length: > 0 } ? sg[0] : double.NaN):0.0} " +
                      $"gain3={(sg is { Length: > 3 } ? sg[3] : double.NaN):0.0} " +
                      $"slider0={_eqSliders[0].Value:0.0} slider3={_eqSliders[3].Value:0.0}");

                var svSet = FindScrollViewer(SettingsView);
                if (svSet is not null)
                {
                    EqBands.StartBringIntoView(new BringIntoViewOptions { VerticalAlignmentRatio = 0.35 });
                    await Task.Delay(800);
                    Trace($"audit settings eq-geom viewport={svSet.ViewportWidth:0} " +
                          $"panel={SettingsView.ActualWidth:0} bands={EqBands.ActualWidth:0} " +
                          $"slider={_eqSliders[0].ActualWidth:0}x{_eqSliders[0].ActualHeight:0}");
                    await ShotAsync("11a-settings-eq.png");

                    svSet.ChangeView(null, svSet.ScrollableHeight, null, true);
                    await Task.Delay(800);
                    await ShotAsync("11b-settings-bottom.png");
                    svSet.ChangeView(null, 0, null, true);
                    await Task.Delay(300);

                    // 桌面歌词开关走真实 Toggled 链路：建窗、落盘，再关掉并确认清干净。
                    SetDesktopLyric.IsOn = true;
                    await Task.Delay(900);
                    var miniOn = await _api!.GetAsync<SettingsResponse>("settings");
                    Trace($"audit mini-toggle on={_mini is not null} saved={miniOn.Settings.DesktopLyric} " +
                          $"top={miniOn.Settings.DesktopLyricOnTop}");
                    SetDesktopLyricTop.IsOn = false;
                    await Task.Delay(700);
                    SetDesktopLyricTop.IsOn = true;
                    await Task.Delay(500);
                    SetDesktopLyric.IsOn = false;
                    await Task.Delay(900);
                    var miniOff = await _api!.GetAsync<SettingsResponse>("settings");
                    Trace($"audit mini-toggle off={_mini is null} saved={miniOff.Settings.DesktopLyric} " +
                          $"top={miniOff.Settings.DesktopLyricOnTop}");
                }
            }
            catch (Exception ex)
            {
                Trace("audit settings-error " + ex.Message);
            }

            // 播放队列浮层：RenderTargetBitmap 渲染弹层会报 E_INVALIDARG，
            // 把面板临时挂回主树、裹上同款玻璃底拍一张，再装回浮层验证打开状态。
            try
            {
                QueueFlyout.Content = null;
                await Task.Delay(150);

                var host = new Border
                {
                    Background = (Brush)Application.Current.Resources["GlassRaisedBrush"],
                    BorderBrush = (Brush)Application.Current.Resources["HairlineStrongBrush"],
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(16),
                    Padding = new Thickness(16, 14, 16, 14),
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = QueuePanel,
                };
                RootGrid.Children.Add(host);
                QueueList.ItemsSource = _player?.Queue;
                QueueCount.Text = _player is null ? "" : $"{_player.Queue.Count} 首";
                await Task.Delay(250);
                if (_player?.Current is { } cur)
                    QueueList.ScrollIntoView(cur, ScrollIntoViewAlignment.Leading);
                await Task.Delay(500);
                Trace($"audit queue preview items={QueueList.Items.Count} " +
                      $"panel={QueuePanel.ActualWidth:0}x{QueuePanel.ActualHeight:0} count=\"{QueueCount.Text}\"");
                await ShotAsync("12-queue.png", host);

                RootGrid.Children.Remove(host);
                host.Child = null;
                QueueFlyout.Content = QueuePanel;
                await Task.Delay(200);

                QueueFlyout.ShowAt(QueueButton);
                await Task.Delay(700);
                var queueScroll = FindScrollViewer(QueueList);
                Trace($"audit queue open={QueueFlyout.IsOpen} items={QueueList.Items.Count} " +
                      $"panel={QueuePanel.ActualWidth:0}x{QueuePanel.ActualHeight:0} count=\"{QueueCount.Text}\" " +
                      $"offset={queueScroll?.VerticalOffset:0}");
                QueueFlyout.Hide();
                await Task.Delay(300);
            }
            catch (Exception ex)
            {
                Trace("audit queue-error " + ex.Message);
            }

            // ---- 新功能巡检：右键菜单 / 队管 / 歌单 / 歌词 / 排除目录 / 排序 / 定时关闭 ----
            try
            {
                EnsureTrackMenu();
                if (_view.Count > 0) await FillPlaylistSubmenuAsync(_view[0]);
                if (_trackMenu is null || _miPlaylist is null) throw new InvalidOperationException("menu not built");
                Trace($"audit track-menu items={_trackMenu.Items.Count} " +
                      $"playlists={_miPlaylist.Items.Count} view={_view.Count}");

                if (_player is not null && _view.Count > 4)
                {
                    var q0 = _player.Queue.Count;
                    var extra = _view[^1];
                    await _player.EnqueueAsync(extra);
                    var q1 = _player.Queue.Count;
                    await _player.RemoveAtAsync(_player.Queue.Count - 1);
                    var q2 = _player.Queue.Count;
                    var curId = _player.Current?.Id;
                    var pick = _view.FirstOrDefault(t => t.Id != curId) ?? _view[0];
                    await _player.PlayNextAsync(pick);
                    var q3 = _player.Queue.Count;
                    var insIdx = _player.Index + 1;
                    var insId = insIdx < _player.Queue.Count ? _player.Queue[insIdx].Id : -1;
                    await _player.NextAsync();
                    var forced = _player.Current?.Id == pick.Id;
                    Trace($"audit queue-insert idx={_player.Index} ins={insId} pick={pick.Id} " +
                          $"cur={_player.Current?.Id} mode={_player.PlayMode}");
                    var idx = _player.Index;
                    await _player.RemoveAtAsync(idx);
                    Trace($"audit queue-ops base={q0} enqueue={q1} remove={q2} playnext={q3} " +
                          $"forced={forced} idx={idx} now=\"{_player.Current?.TitleText}\" " +
                          $"playing={_player.IsPlaying}");
                }

                var plName = "审计歌单" + DateTime.Now.Second.ToString("00");
                var pidResp = await _api!.PostAsync<PlaylistsResponse>("playlists", new { name = plName });
                var pid = pidResp.Id;
                await _api.PostAsync("playlists/rename", new { id = pid, name = plName + "·改" });
                var ids3 = _view.Take(3).Select(t => t.Id).ToArray();
                await _api.PostAsync($"playlists/{pid}/add", new { track_ids = ids3 });
                await _api.PostAsync($"playlists/{pid}/move", new { track_id = ids3[0], position = 2 });
                await _api.PostAsync($"playlists/{pid}/remove", new { track_id = ids3[0] });
                await _api.PostAsync("playlists/pin", new { id = pid, pinned = true });
                var plTracks = await _api.GetAsync<TracksResponse>($"playlist/{pid}?limit=5000");
                var plExport = await _api.PostAsync<M3uExportResponse>("playlists/export", new { id = pid });
                await _api.PostAsync("playlists/delete", new { id = pid });
                await RefreshPlaylistsAsync();
                Trace($"audit playlists id={pid} remain={plTracks.Items.Count}/{plTracks.Total} " +
                      $"export={plExport.Ok}/{plExport.Count} missing={plExport.Missing} path=\"{plExport.Path}\"");

                var lyrTrack = _view.Count > 0
                    ? (_view.FirstOrDefault(t => t.Id != _player?.Current?.Id) ?? _view[0])
                    : null;
                if (lyrTrack is null)
                {
                    Trace("audit lyric-save skipped: empty view");
                }
                else
                {
                    var lyrSave = await _api.PostAsync<LyricEditResponse>("lyrics/save",
                        new { track_id = lyrTrack.Id, text = "[00:00.00]审计第一行\n[00:02.50]审计第二行" });
                    var lyrBack = await _api.GetAsync<LyricsResponse>($"lyrics?track_id={lyrTrack.Id}");
                    Trace($"audit lyric-save ok={lyrSave.Ok} lines={lyrBack.Lines.Count} " +
                          $"plain={lyrBack.Plain} source=\"{lyrBack.Source}\"");
                }

                var dataDir = Environment.GetEnvironmentVariable("SALTPLAYER_DATA") ?? "";
                var probeDir = System.IO.Path.Combine(dataDir, "exclude-probe");
                Directory.CreateDirectory(probeDir);
                var exAdd = await _api.PostAsync<ExcludesResponse>("excludes/add", new { path = probeDir });
                var exRemove = await _api.PostAsync<ExcludesResponse>("excludes/remove", new { path = probeDir });
                Directory.Delete(probeDir, true);
                Trace($"audit excludes add={exAdd.Ok}/{exAdd.Items.Count}/{exAdd.Already} " +
                      $"remove={exRemove.Ok}/{exRemove.Items.Count}");

                var scan = await _api.GetAsync<ScanStatusResponse>("scan");
                Trace($"audit scan-status running={scan.Running} phase={scan.Phase} " +
                      $"total={scan.Total} done={scan.Done}");

                await GoToAsync("songs");
                await Task.Delay(600);
                SelectByTag(SortBox, "bitrate");
                await Task.Delay(800);
                var bFirst = _view.FirstOrDefault()?.Bitrate ?? 0;
                var bLast = _view.LastOrDefault()?.Bitrate ?? 0;
                SortDir_Click(SortDirButton, new RoutedEventArgs());
                await Task.Delay(600);
                var bFlipped = _view.FirstOrDefault()?.Bitrate ?? 0;
                Trace($"audit sort bitrate desc-first={bFirst} desc-last={bLast} " +
                      $"flip-first={bFlipped} asc={!_sortDesc} n={_view.Count}");
                SelectByTag(SortBox, "default");
                await Task.Delay(500);

                if (_player is { IsPlaying: true })
                {
                    _sleepDeadline = DateTime.UtcNow.AddMilliseconds(400);
                    _sleepAfterTrack = false;
                    _sleepTicks = 0;
                    if (!_sleepTimer.IsEnabled) _sleepTimer.Start();
                    await Task.Delay(1800);
                    var stopped = !_player.IsPlaying;
                    Trace($"audit sleep paused={stopped} timer={_sleepTimer.IsEnabled}");
                    if (stopped) await _player.ToggleAsync();
                }
            }
            catch (Exception ex)
            {
                Trace("audit features-error " + ex.Message);
            }

            // 晚一点再回首页拍一张：验证首屏封面（尤其后端现生成的色块）都已落地
            await GoToAsync("home");
            await Task.Delay(1200);
            await ShotAsync("10-home-late.png");

            // 浅色主题：走真实的设置下拉回调（会落盘），再看玻璃还有没有层次。
            try
            {
                SelectByTag(SetTheme, "light");
                await Task.Delay(1100);
                var savedTheme = await _api!.GetAsync<SettingsResponse>("settings");
                Trace($"audit light theme=\"{ComboTag(SetTheme)}\" saved=\"{savedTheme.Settings?.Theme}\" " +
                      $"root={RootGrid.RequestedTheme} noise={NoiseLayer.Opacity:0.00}");
                await ShotAsync("13-light-home.png");

                await GoToAsync("songs");
                await Task.Delay(800);
                await ShotAsync("13a-light-songs.png");

                await GoToAsync("settings");
                await Task.Delay(800);
                await ShotAsync("13b-light-settings.png");
                SettingsView.ChangeView(null, 0, null, true);

                await GoToAsync("home");
                await Task.Delay(400);
                OpenLyrics();
                await Task.Delay(1400);
                await ShotAsync("13c-light-lyrics.png");
                CloseLyrics();
                await Task.Delay(300);

                ShowMiniLyrics(persist: false);
                await Task.Delay(1000);
                await ShotAsync("14-mini-light.png", _mini?.Root);
                HideMiniLyrics(persist: false);
                await Task.Delay(300);

                SelectByTag(SetTheme, "dark");
                await Task.Delay(900);
                await ShotAsync("16-dark-home.png");
                await GoToAsync("songs");
                await Task.Delay(800);
                await ShotAsync("16a-dark-songs.png");
                await GoToAsync("albums");
                await Task.Delay(900);
                await ShotAsync("16b-dark-albums.png");
                await GoToAsync("settings");
                await Task.Delay(800);
                await ShotAsync("16c-dark-settings.png");
                SettingsView.ChangeView(null, 0, null, true);
                await GoToAsync("home");
                await Task.Delay(400);
                OpenLyrics();
                await Task.Delay(1400);
                await ShotAsync("16d-dark-lyrics.png");
                CloseLyrics();
                await Task.Delay(300);

                ShowMiniLyrics(persist: false);
                await Task.Delay(900);
                await ShotAsync("15-mini-dark.png", _mini?.Root);
                HideMiniLyrics(persist: false);
                await Task.Delay(200);

                SelectByTag(SetTheme, bootTheme);
                await Task.Delay(700);
                Trace($"audit theme restored=\"{ComboTag(SetTheme)}\" root={RootGrid.RequestedTheme}");
            }
            catch (Exception ex)
            {
                Trace("audit light-error " + ex.Message);
            }

            // 空状态与最后一条队列移除：界面上必须有明确提示，而不是整片空白。
            try
            {
                await GoToAsync("albums");
                await Task.Delay(500);
                SearchBox.Text = "zzzz-no-match";
                await Task.Delay(650);
                Trace($"audit empty albums n={AlbumGrid.Items.Count} overlay={AlbumsEmpty.Visibility}");
                await ShotAsync("17-albums-empty.png");
                await GoToAsync("artists");
                await Task.Delay(500);
                Trace($"audit empty artists n={ArtistGrid.Items.Count} overlay={ArtistsEmpty.Visibility}");
                await ShotAsync("17a-artists-empty.png");
                SearchBox.Text = "";
                await Task.Delay(400);

                if (_player is not null && _player.Queue.Count > 0)
                {
                    var keep = _player.Current;
                    var keepIdx = keep is null ? 0 : Math.Max(0, _view.IndexOf(keep));
                    while (_player.Queue.Count > 0) await _player.RemoveAtAsync(0);
                    QueueFlyout.ShowAt(QueueButton);
                    await Task.Delay(500);
                    Trace($"audit empty queue={_player.Queue.Count} overlay={QueueEmpty.Visibility} " +
                          $"clearEnabled={QueueClearButton.IsEnabled}");
                    await ShotAsync("17b-queue-empty.png", QueuePanel);
                    QueueFlyout.Hide();
                    await Task.Delay(300);
                    if (keep is not null && _view.Count > 0)
                        await _player.PlayFromAsync(_view, keepIdx, _context);
                }
            }
            catch (Exception ex)
            {
                Trace("audit empty-error " + ex.Message);
            }

            // 单实例守卫：第二次启动必须立刻退出，而不是再开一套后端。
            try
            {
                var exe = Environment.ProcessPath;
                if (!string.IsNullOrEmpty(exe))
                {
                    var psi = new System.Diagnostics.ProcessStartInfo(exe) { UseShellExecute = false };
                    foreach (var key in new[] { "SALTPLAYER_DATA", "SALTPLAYER_SILENCE", "SALTPLAYER_NATIVE_TRACE" })
                    {
                        var value = Environment.GetEnvironmentVariable(key);
                        if (!string.IsNullOrEmpty(value)) psi.Environment[key] = value;
                    }
                    using var second = System.Diagnostics.Process.Start(psi);
                    var exited = second?.WaitForExit(12000) == true;
                    Trace($"audit single-instance second_exited={exited} " +
                          $"code={(exited ? second!.ExitCode.ToString() : "-")}");
                    if (!exited)
                    {
                        try { second?.Kill(entireProcessTree: true); } catch { }
                    }
                }
            }
            catch (Exception ex)
            {
                Trace("audit single-instance-error " + ex.Message);
            }

            Trace("audit-done");
        }
        catch (Exception e)
        {
            Trace("audit-error " + e);
        }
        finally
        {
            CompositionTarget.Rendering -= AuditRendering;
        }
    }

    private async Task ShotAsync(string name, FrameworkElement? element = null)
    {
        if (_auditDir is null) return;
        try
        {
            await CaptureAsync(Path.Combine(_auditDir, name), element);
            Trace("audit shot " + name);
        }
        catch (Exception e)
        {
            Trace("audit shot-error " + name + " " + e.Message);
        }
    }

    private static ScrollViewer? FindScrollViewer(DependencyObject root)
    {
        if (root is ScrollViewer sv) return sv;
        var count = VisualTreeHelper.GetChildrenCount(root);
        for (var i = 0; i < count; i++)
        {
            var found = FindScrollViewer(VisualTreeHelper.GetChild(root, i));
            if (found is not null) return found;
        }
        return null;
    }

    private void AuditRendering(object? sender, object e)
    {
        var now = _auditClock.Elapsed.TotalMilliseconds;
        var dt = now - _auditLast;
        _auditLast = now;
        if (!_auditScrolling || dt <= 0 || dt >= 250) return;
        _auditFrames.Add(dt);
        _auditOffsets.Add(_auditScroll?.VerticalOffset ?? 0);
    }

    private async Task ScrollStepsAsync(ScrollViewer sv, double from, double step, int ms, string name)
    {
        BeginMeasure();
        var start = _auditClock.Elapsed.TotalMilliseconds;
        var target = from;
        var max = sv.ScrollableHeight;
        while (_auditClock.Elapsed.TotalMilliseconds - start < ms)
        {
            target = target + step > max ? 0 : target + step;
            sv.ChangeView(null, target, null, true);
            await Task.Delay(16);
        }
        EndMeasure(name);
    }

    private async Task FlingAsync(ScrollViewer sv, double to, int ms, string name)
    {
        BeginMeasure();
        sv.ChangeView(null, to, null, false);
        await Task.Delay(ms);
        EndMeasure(name);
    }

    private void BeginMeasure()
    {
        _auditFrames.Clear();
        _auditOffsets.Clear();
        _auditLast = _auditClock.Elapsed.TotalMilliseconds;
        _auditScrolling = true;
    }

    private void EndMeasure(string name)
    {
        _auditScrolling = false;
        var sorted = _auditFrames.OrderBy(x => x).ToArray();
        double Pct(double p) => sorted.Length == 0
            ? 0 : sorted[Math.Min(sorted.Length - 1, (int)(p * sorted.Length))];
        Trace($"audit {name}: n={sorted.Length} p50={Pct(.5):0.00} p95={Pct(.95):0.00} " +
              $"max={Pct(1):0.00} gt33={_auditFrames.Count(x => x > 33)} " +
              $"gt50={_auditFrames.Count(x => x > 50)} top={_auditOffsets.LastOrDefault():0}");
        Trace("audit traj=" + string.Join(",",
            _auditOffsets.Where((_, i) => i % 4 == 0).Take(40).Select(x => ((int)x).ToString())));
    }

    private void ApplyBootstrap(BootstrapResponse boot)
    {
        var stats = boot.Stats;
        _favCount = stats.Favorites;

        LibraryName.Text = boot.Libraries.FirstOrDefault()?.DisplayName ?? "音乐文件夹";
        LibrarySub.Text = $"{stats.Tracks:N0} 首 · {FormatLong(stats.Duration)}";
        NavSongsCount.Text = stats.Tracks.ToString("N0");
        NavFavCount.Text = stats.Favorites.ToString("N0");
        NavAlbumsCount.Text = stats.Albums.ToString("N0");
        PlaylistNav.ItemsSource = boot.Playlists;

        HeroStatA.Text = $"{stats.Tracks:N0} 首歌曲";
        HeroStatB.Text = $"{stats.Artists:N0} 位艺术家 · {stats.Albums:N0} 张专辑";

        ViewTitle.Text = "此刻";
        ViewSub.Text = $"{stats.Tracks:N0} 首歌曲 · {FormatLong(stats.Duration)}";
    }

    private async Task LoadLibraryAsync()
    {
        var api = _api!;
        var tracksTask = api.GetAsync<TracksResponse>("tracks?limit=5000&order=default");
        var albumsTask = api.GetAsync<AlbumsResponse>("albums?limit=5000");
        var artistsTask = api.GetAsync<ArtistsResponse>("artists?limit=5000");
        var recentTask = api.GetAsync<TracksResponse>("history?limit=12");
        var freshTask = api.GetAsync<TracksResponse>("tracks?order=added&limit=12");
        await Task.WhenAll(tracksTask, albumsTask, artistsTask, recentTask, freshTask);

        _allTracks.AddRange(tracksTask.Result.Items.Select(Unify).Distinct());
        foreach (var t in _allTracks) PrepareTrack(t);

        _recent.AddRange(recentTask.Result.Items.Select(Unify).Distinct());
        foreach (var t in _recent) PrepareTrack(t);

        _fresh.AddRange(freshTask.Result.Items.Select(Unify));
        foreach (var t in _fresh) PrepareTrack(t);

        // 首页一屏只放一排 6 张；列表本身仍保留 12 首，点卡片时按完整列表建队列。
        RecentCards.ItemsSource = _recent.Take(6).ToList();
        NewCards.ItemsSource = _fresh.Take(6).ToList();

        // 「单曲」是没有专辑标签的占位桶，排在正常专辑后面，首页/专辑页首屏先给真专辑。
        _albumsAll.AddRange(albumsTask.Result.Items
            .OrderBy(a => a.Album == "单曲" || a.Album == "未知专辑" ? 1 : 0));
        foreach (var a in _albumsAll) a.CoverUrl = api.AlbumCover(a);

        _artistsAll.AddRange(artistsTask.Result.Items);
        foreach (var a in _artistsAll) a.CoverUrl = api.ArtistCover(a);

        _homeAlbums = _albumsAll.Take(14).ToList();
        HomeAlbums.ItemsSource = _homeAlbums;

        RecentSection.Visibility = _recent.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        NewSection.Visibility = _fresh.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        HomeAlbumsSection.Visibility = _homeAlbums.Count > 0 ? Visibility.Visible : Visibility.Collapsed;

        AlbumGrid.ItemsSource = _albumsAll;
        ArtistGrid.ItemsSource = _artistsAll;

        SyncHero();
    }

    private Track Unify(Track t)
    {
        if (_trackCache.TryGetValue(t.Id, out var cached))
        {
            cached.Favorite = t.Favorite;
            return cached;
        }
        _trackCache[t.Id] = t;
        return t;
    }

    private void PrepareTrack(Track t)
    {
        if (_api is not null && t.Id > 0) t.CoverUrl = _api.TrackCover(t.Id, "thumb");
    }

    private void SetBootStatus(string text) => BootStatus.Text = text;

    private void ShowBootError(string message)
    {
        Trace("boot-error " + message);
        BootRing.IsActive = false;
        BootStatus.Text = "无法启动";
        BootError.Text = message;
        BootError.Visibility = Visibility.Visible;
        OpenLogButton.Visibility = _backend is null ? Visibility.Collapsed : Visibility.Visible;
    }

    private DispatcherQueueTimer? _bootHideTimer;

    private void HideBoot()
    {
        // 这层遮罩盖住整个内容区。退场只靠 Storyboard.Completed 是不够的：
        // 用户在 420ms 淡出期间把窗口最小化时 Completed 可能不来，遮罩会以
        // Opacity=0 + Visible 留下——界面看得见，内容区却整片点不动。
        // 所以先关命中，再用定时器兜底折叠；动画只负责好看，不承担正确性。
        BootOverlay.IsHitTestVisible = false;
        var fade = new DoubleAnimation
        {
            To = 0,
            Duration = TimeSpan.FromMilliseconds(420),
            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
        };
        Storyboard.SetTarget(fade, BootOverlay);
        Storyboard.SetTargetProperty(fade, "Opacity");
        var story = new Storyboard();
        story.Children.Add(fade);
        story.Completed += (_, _) => CollapseBootOverlay();
        story.Begin();

        if (_bootHideTimer is null)
        {
            _bootHideTimer = DispatcherQueue.CreateTimer();
            _bootHideTimer.IsRepeating = false;
            _bootHideTimer.Tick += (_, _) => CollapseBootOverlay();
        }
        _bootHideTimer.Interval = TimeSpan.FromMilliseconds(700);
        _bootHideTimer.Start();
        Trace("boot-hide");
    }

    private void CollapseBootOverlay()
    {
        _bootHideTimer?.Stop();
        if (BootOverlay.Visibility == Visibility.Collapsed) return;
        BootOverlay.Visibility = Visibility.Collapsed;
        Trace("boot-hidden");
    }

    // ------------------------------------------------------------- navigation

    private async void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        CloseLyrics();
        var tag = (sender as FrameworkElement)?.Tag as string;
        if (string.IsNullOrEmpty(tag)) return;
        _activeNav = tag;
        await NavigateAsync(tag);
    }

    private async Task GoToAsync(string tag)
    {
        SetNavChecked(tag);
        _activeNav = tag;
        await NavigateAsync(tag);
    }

    private void SetNavChecked(string tag)
    {
        var radio = tag switch
        {
            "home" => NavHome,
            "favorites" => NavFavorites,
            "history" => NavHistory,
            "radio" => NavRadio,
            "songs" => NavSongs,
            "albums" => NavAlbums,
            "artists" => NavArtists,
            "folders" => NavFolders,
            "genres" => NavGenres,
            "settings" => NavSettings,
            _ => null,
        };
        if (radio is not null && radio.IsChecked != true) radio.IsChecked = true;
    }

    private async Task NavigateAsync(string tag)
    {
        try
        {
            switch (tag)
            {
                case "home":
                    ShowHome();
                    break;
                case "songs":
                    ShowSongs();
                    break;
                case "favorites":
                    await ShowFavoritesAsync();
                    break;
                case "history":
                    await ShowHistoryAsync();
                    break;
                case "radio":
                    await StartRadioAsync();
                    break;
                case "albums":
                    ShowAlbums();
                    break;
                case "artists":
                    ShowArtists();
                    break;
                case "folders":
                    await ShowFoldersAsync();
                    break;
                case "genres":
                    await ShowGenresAsync();
                    break;
                case "settings":
                    await ShowSettingsAsync();
                    break;
            }
        }
        catch (Exception e)
        {
            ShowToast("加载失败：" + e.Message);
        }
    }

    private void ShowOnly(FrameworkElement view)
    {
        AlbumsEmpty.Visibility = Visibility.Collapsed;
        ArtistsEmpty.Visibility = Visibility.Collapsed;
        CategoriesEmpty.Visibility = Visibility.Collapsed;
        HomeView.Visibility = ReferenceEquals(view, HomeView) ? Visibility.Visible : Visibility.Collapsed;
        TracksView.Visibility = ReferenceEquals(view, TracksView) ? Visibility.Visible : Visibility.Collapsed;
        AlbumGrid.Visibility = ReferenceEquals(view, AlbumGrid) ? Visibility.Visible : Visibility.Collapsed;
        ArtistGrid.Visibility = ReferenceEquals(view, ArtistGrid) ? Visibility.Visible : Visibility.Collapsed;
        CategoryGrid.Visibility = ReferenceEquals(view, CategoryGrid) ? Visibility.Visible : Visibility.Collapsed;
        SettingsView.Visibility = ReferenceEquals(view, SettingsView) ? Visibility.Visible : Visibility.Collapsed;
        PlaceholderView.Visibility = ReferenceEquals(view, PlaceholderView) ? Visibility.Visible : Visibility.Collapsed;
    }

    private void ConfigureHeader(string title, string sub, bool sort, bool actions)
    {
        ViewTitle.Text = title;
        ViewSub.Text = sub;
        ViewTitle.Visibility = Visibility.Visible;
        ViewSub.Visibility = Visibility.Visible;
        DetailHero.Visibility = Visibility.Collapsed;
        SearchHost.Visibility = Visibility.Visible;
        SortBox.Visibility = sort ? Visibility.Visible : Visibility.Collapsed;
        SortDirButton.Visibility = sort ? Visibility.Visible : Visibility.Collapsed;
        PlayAllButton.Visibility = actions ? Visibility.Visible : Visibility.Collapsed;
        ShuffleButton.Visibility = actions ? Visibility.Visible : Visibility.Collapsed;
        BackButton.Visibility = Visibility.Collapsed;
        AzToggle.Visibility = _viewMode == "songs" ? Visibility.Visible : Visibility.Collapsed;
        AzToggle.Foreground = _azMode ? _accentBrush : _dimBrush;
        AzToggle.Background = _azMode ? _accentSoftBrush : _controlFillBrush;
        AzToggle.BorderBrush = _azMode ? _accentBrush : _controlStrokeBrush;
    }

    private void ShowDetailHero(string kicker, string title, string meta, string? coverUrl, bool round = false)
    {
        // 详情页用大封面代替页头的重复标题：页头只留返回与右侧工具，视觉重心交给 Hero。
        ViewTitle.Visibility = Visibility.Collapsed;
        ViewSub.Visibility = Visibility.Collapsed;
        DetailKicker.Text = kicker;
        DetailTitle.Text = title;
        DetailMeta.Text = meta;
        DetailArtBorder.CornerRadius = new CornerRadius(round ? 66 : 20);
        DetailArt.Tag = null;
        DetailArt.Source = string.IsNullOrWhiteSpace(coverUrl)
            ? null
            : new BitmapImage { DecodePixelWidth = 320, UriSource = new Uri(coverUrl) };
        DetailHero.Visibility = Visibility.Visible;
    }

    private static string TotalDuration(IEnumerable<Track> tracks)
    {
        var seconds = tracks.Sum(t => t.Duration ?? 0);
        return FormatLong(seconds);
    }

    private void ShowHome()
    {
        _viewMode = "home";
        _context = "首页";
        _backTarget = "home";
        ShowOnly(HomeView);
        var stats = _boot?.Stats;
        ConfigureHeader("此刻",
            stats is null ? "正在载入曲库…" : $"{stats.Tracks:N0} 首歌曲 · {FormatLong(stats.Duration)}",
            false, false);
        SyncHero();
    }

    private void ShowSongs()
    {
        _viewMode = "songs";
        _base = _azMode && _azTracks.Count > 0 ? _azTracks : _allTracks;
        _context = "所有歌曲";
        _subPrefix = _azMode ? "A–Z · " : "";
        _backTarget = "home";
        ShowOnly(TracksView);
        ConfigureHeader("所有歌曲", "", sort: true, actions: true);
        SortBox.IsEnabled = !_azMode;
        SortDirButton.IsEnabled = !_azMode;
        if (_azMode)
        {
            _syncingSort = true;
            SortBox.SelectedIndex = 0;
            _syncingSort = false;
            _sortMode = "default";
            _sortDesc = false;
            UpdateSortDirUi();
        }
        ApplyFilter();
        UpdateAzRail();
    }

    private async void AzToggle_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready || _viewMode != "songs") return;
        if (!_azMode)
        {
            try
            {
                if (_azTracks.Count == 0) await LoadAzDataAsync();
                _azMode = true;
            }
            catch (Exception ex)
            {
                ShowToast("A–Z 索引加载失败：" + ex.Message);
                return;
            }
        }
        else
        {
            _azMode = false;
        }
        ShowSongs();
    }

    private async Task LoadAzDataAsync()
    {
        var tracksTask = _api!.GetAsync<TracksResponse>("tracks?limit=5000&order=az");
        var indexTask = _api!.GetAsync<IndexResponse>("index");
        await Task.WhenAll(tracksTask, indexTask);

        _azTracks = tracksTask.Result.Items.Select(Unify).ToList();
        foreach (var t in _azTracks) PrepareTrack(t);

        var byLetter = indexTask.Result.Blocks
            .GroupBy(b => b.Letter)
            .ToDictionary(g => g.Key, g => g.First());
        var letters = new List<AzLetter>(27);
        foreach (var l in "ABCDEFGHIJKLMNOPQRSTUVWXYZ#")
        {
            letters.Add(byLetter.TryGetValue(l.ToString(), out var b)
                ? new AzLetter { Letter = l.ToString(), Offset = b.Offset, Count = b.Count }
                : new AzLetter { Letter = l.ToString(), Offset = 0, Count = 0 });
        }
        _azLetters = letters;
        _azActive = null;
        AzRail.ItemsSource = _azLetters;
    }

    private void AzLetter_Click(object sender, RoutedEventArgs e)
    {
        if (!_azMode || (sender as FrameworkElement)?.DataContext is not AzLetter letter) return;
        if (letter.Count <= 0 || letter.Offset < 0 || letter.Offset >= _view.Count) return;
        SetAzActive(letter);
        ShowAzBubble(letter.Letter);
        TrackList.ScrollIntoView(_view[letter.Offset], ScrollIntoViewAlignment.Leading);
    }

    private void SetAzActive(AzLetter letter)
    {
        if (ReferenceEquals(_azActive, letter)) return;
        if (_azActive is not null) _azActive.Active = false;
        _azActive = letter;
        letter.Active = true;
    }

    private void ShowAzBubble(string letter)
    {
        AzBubbleText.Text = letter;
        AzBubble.Visibility = Visibility.Visible;
        _azBubbleTimer.Stop();
        _azBubbleTimer.Start();
    }

    private void UpdateAzRail()
    {
        var show = _azMode && _viewMode == "songs" && SearchBox.Text.Trim().Length == 0;
        AzRailHost.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        // 字母轨贴在右缘，给行尾留出 28px，喜欢按钮不会被盖住
        TrackList.Padding = new Thickness(0, 0, show ? 38 : 10, 12);
        TrackHeaderRow.Padding = new Thickness(10, 0, show ? 50 : 22, 0);
        if (!show)
        {
            DetachAzScroll();
            AzBubble.Visibility = Visibility.Collapsed;
            return;
        }
        if (_azScroll is not null) SyncAzActiveFromOffset();
        _ = AttachAzScrollAsync();
    }

    private async Task AttachAzScrollAsync()
    {
        await Task.Delay(80);
        if (!_azMode || _viewMode != "songs") return;
        var sv = _azScroll ?? FindScrollViewer(TrackList);
        if (sv is null) return;
        if (ReferenceEquals(_azScroll, sv)) return;
        DetachAzScroll();
        _azScroll = sv;
        sv.ViewChanged += AzScroll_ViewChanged;
        SyncAzActiveFromOffset();
    }

    private void DetachAzScroll()
    {
        if (_azScroll is null) return;
        _azScroll.ViewChanged -= AzScroll_ViewChanged;
        _azScroll = null;
    }

    private void AzScroll_ViewChanged(object? sender, ScrollViewerViewChangedEventArgs e) =>
        SyncAzActiveFromOffset();

    private void SyncAzActiveFromOffset()
    {
        if (!_azMode || _view.Count == 0 || _azLetters.Count == 0 || _azScroll is null) return;
        // 行高固定 56 + 上下各 1px 边距；ListView 虚拟化时也只有这一种行高
        var index = (int)Math.Round(_azScroll.VerticalOffset / 58.0);
        index = Math.Clamp(index, 0, _view.Count - 1);
        AzLetter? found = null;
        foreach (var letter in _azLetters)
        {
            if (letter.Count > 0 && letter.Offset <= index) found = letter;
        }
        if (found is not null) SetAzActive(found);
    }

    private async Task ShowFavoritesAsync()
    {
        var r = await _api!.GetAsync<TracksResponse>("favorites?limit=5000");
        _favorites.Clear();
        _favorites.AddRange(r.Items.Select(Unify));
        foreach (var t in _favorites) PrepareTrack(t);
        _favCount = r.Total > 0 ? r.Total : _favorites.Count;
        NavFavCount.Text = _favCount.ToString("N0");

        _viewMode = "favorites";
        _base = _favorites;
        _context = "我喜欢的";
        _subPrefix = "";
        _backTarget = "home";
        ShowOnly(TracksView);
        ConfigureHeader("我喜欢的", "", sort: true, actions: true);
        ApplyFilter();
    }

    private async Task ShowHistoryAsync()
    {
        var r = await _api!.GetAsync<TracksResponse>("history?limit=100");
        _history.Clear();
        _history.AddRange(r.Items.Select(Unify));
        foreach (var t in _history) PrepareTrack(t);

        _viewMode = "history";
        _base = _history;
        _context = "最近播放";
        _subPrefix = "";
        _backTarget = "home";
        ShowOnly(TracksView);
        ConfigureHeader("最近播放", "", sort: true, actions: true);
        ApplyFilter();
    }

    private async Task StartRadioAsync()
    {
        if (_allTracks.Count == 0)
        {
            ShowToast("曲库是空的，先添加音乐文件夹");
            return;
        }
        var picks = _allTracks.OrderBy(_ => Random.Shared.Next()).Take(80).ToList();
        _viewMode = "radio";
        _base = picks;
        _context = "随意电台";
        _subPrefix = "随机抽取 · ";
        _backTarget = "home";
        ShowOnly(TracksView);
        ConfigureHeader("随意电台", "", sort: false, actions: true);
        ApplyFilter();
        if (_player is null) return;
        await _player.SetPlayModeAsync("shuffle");
        await _player.PlayFromAsync(_view, 0, _context);
    }

    private void ShowAlbums()
    {
        _viewMode = "albums";
        _context = "专辑";
        _backTarget = "home";
        ShowOnly(AlbumGrid);
        ConfigureHeader("专辑", "", false, false);
        FilterAlbums();
    }

    private void ShowArtists()
    {
        _viewMode = "artists";
        _context = "艺术家";
        _backTarget = "home";
        ShowOnly(ArtistGrid);
        ConfigureHeader("艺术家", "", false, false);
        FilterArtists();
    }

    private async Task ShowFoldersAsync()
    {
        var folders = (await _api!.GetAsync<FoldersResponse>("folders?flat=1")).Items;
        _categories.Clear();
        foreach (var f in folders)
        {
            _categories.Add(new CategoryInfo
            {
                Title = f.Name,
                Kind = "folder",
                Key = f.Path,
                Icon = "\uE8B7",
                Subtitle = $"{f.Tracks:N0} 首 · {FormatLong(f.Duration)}",
                CoverUrl = f.CoverTrack > 0
                    ? _api.TrackCover(f.CoverTrack, "art")
                    : _api.TextCover(f.Name, ""),
            });
        }
        _viewMode = "folders";
        _context = "文件夹";
        _backTarget = "home";
        ShowOnly(CategoryGrid);
        ConfigureHeader("文件夹", "", false, false);
        FilterCategories();
    }

    private async Task ShowGenresAsync()
    {
        var genres = (await _api!.GetAsync<GenresResponse>("genres")).Items;
        _categories.Clear();
        foreach (var g in genres)
        {
            _categories.Add(new CategoryInfo
            {
                Title = g.Name,
                Kind = "genre",
                Key = g.Name,
                Icon = "\uE8EC",
                Subtitle = $"{g.Tracks:N0} 首 · {FormatLong(g.Duration)}",
                CoverUrl = _api.TextCover(g.Name, ""),
            });
        }
        _viewMode = "genres";
        _context = "流派";
        _backTarget = "home";
        ShowOnly(CategoryGrid);
        ConfigureHeader("流派", "", false, false);
        FilterCategories();
    }

    private void FilterCategories()
    {
        var term = SearchBox.Text.Trim();
        var list = term.Length == 0
            ? _categories.ToList()
            : _categories.Where(c =>
                c.Title.Contains(term, StringComparison.CurrentCultureIgnoreCase) ||
                c.Key.Contains(term, StringComparison.CurrentCultureIgnoreCase)).ToList();
        CategoryGrid.ItemsSource = list;
        CategoriesEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        CategoriesEmptyText.Text = term.Length > 0
            ? $"没有找到“{term}”"
            : _viewMode == "folders" ? "这里还没有文件夹" : "这里还没有流派";
        var total = term.Length > 0 ? $" / {_categories.Count:N0}" : "";
        ViewSub.Text = _viewMode == "folders"
            ? $"{list.Count:N0}{total} 个文件夹"
            : $"{list.Count:N0}{total} 种流派";
    }

    private async Task ShowCategoryTracksAsync(CategoryInfo category)
    {
        var url = category.Kind == "folder"
            ? "tracks?limit=5000&order=default&folder=" + Uri.EscapeDataString(category.Key)
            : "tracks?limit=5000&order=album&genre=" + Uri.EscapeDataString(category.Key);
        var r = await _api!.GetAsync<TracksResponse>(url);
        _viewMode = category.Kind == "folder" ? "folder" : "genre";
        _base = r.Items.Select(Unify).ToList();
        foreach (var t in _base) PrepareTrack(t);
        _context = category.Title;
        _subPrefix = "";
        _backTarget = category.Kind == "folder" ? "folders" : "genres";
        ShowOnly(TracksView);
        ConfigureHeader(category.Title, "", sort: true, actions: true);
        BackButton.Visibility = Visibility.Visible;
        ShowDetailHero(category.Kind == "folder" ? "文件夹" : "流派", category.Title,
            category.Subtitle, category.CoverUrl);
        ApplyFilter();
    }

    private async void CategoryGrid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (!_ready || e.ClickedItem is not CategoryInfo category) return;
        try
        {
            await ShowCategoryTracksAsync(category);
        }
        catch (Exception ex)
        {
            ShowToast("加载失败：" + ex.Message);
        }
    }

    private async Task ShowAlbumAsync(AlbumInfo album)
    {
        var url = "tracks?limit=5000&order=album" +
                  "&album=" + Uri.EscapeDataString(album.Album) +
                  "&group_artist=" + Uri.EscapeDataString(album.ArtistText);
        var r = await _api!.GetAsync<TracksResponse>(url);
        _viewMode = "album";
        _base = r.Items.Select(Unify).ToList();
        foreach (var t in _base) PrepareTrack(t);
        _context = album.Album;
        _subPrefix = album.Year is > 0 ? $"{album.ArtistText} · {album.Year} · " : $"{album.ArtistText} · ";
        _backTarget = "albums";
        ShowOnly(TracksView);
        ConfigureHeader(album.Album, "", sort: true, actions: true);
        BackButton.Visibility = Visibility.Visible;
        ShowDetailHero("专辑", album.Album,
            $"{album.ArtistText}{(album.Year is > 0 ? " · " + album.Year : "")} · " +
            $"{_base.Count:N0} 首 · {TotalDuration(_base)}",
            album.CoverUrl);
        ApplyFilter();
    }

    private async Task ShowArtistAsync(ArtistInfo artist)
    {
        var url = "tracks?limit=5000&order=album&group_artist=" + Uri.EscapeDataString(artist.Name);
        var r = await _api!.GetAsync<TracksResponse>(url);
        _viewMode = "artist";
        _base = r.Items.Select(Unify).ToList();
        foreach (var t in _base) PrepareTrack(t);
        _context = artist.Name;
        _subPrefix = $"{artist.Albums:N0} 张专辑 · ";
        _backTarget = "artists";
        ShowOnly(TracksView);
        ConfigureHeader(artist.Name, "", sort: true, actions: true);
        BackButton.Visibility = Visibility.Visible;
        ShowDetailHero("艺术家", artist.Name,
            $"{artist.Albums:N0} 张专辑 · {_base.Count:N0} 首 · {TotalDuration(_base)}",
            artist.CoverUrl, round: true);
        ApplyFilter();
    }

    private async Task ShowPlaylistAsync(PlaylistInfo playlist)
    {
        var r = await _api!.GetAsync<TracksResponse>($"playlist/{playlist.Id}?limit=5000");
        _viewMode = "playlist";
        _playlistId = playlist.Id;
        _base = r.Items.Select(Unify).ToList();
        foreach (var t in _base) PrepareTrack(t);
        _context = playlist.Name;
        _subPrefix = "";
        _backTarget = string.IsNullOrEmpty(_activeNav) ? "home" : _activeNav;
        ShowOnly(TracksView);
        ConfigureHeader(playlist.Name, "", sort: true, actions: true);
        BackButton.Visibility = Visibility.Visible;
        var cover = playlist.CoverTrack is > 0
            ? _api!.TrackCover(playlist.CoverTrack.Value, "art")
            : _api!.TextCover(playlist.Name, "");
        ShowDetailHero("歌单", playlist.Name, $"{_base.Count:N0} 首 · {TotalDuration(_base)}", cover);
        ApplyFilter();
    }

    private async void Back_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        await GoToAsync(_backTarget);
    }

    private async void PlaylistNav_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        if ((sender as FrameworkElement)?.DataContext is not PlaylistInfo playlist) return;
        try
        {
            await ShowPlaylistAsync(playlist);
        }
        catch (Exception ex)
        {
            ShowToast("歌单加载失败：" + ex.Message);
        }
    }

    // ---------------------------------------------------------- list + search

    private void SearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_ready) return;
        if (_viewMode == "home" && SearchBox.Text.Trim().Length > 0)
        {
            _searchDelay.Stop();
            _ = GoToAsync("songs");
            return;
        }
        _searchDelay.Stop();
        _searchDelay.Start();
    }

    private void SortBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_syncingSort) return;
        _sortMode = (SortBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "default";
        _sortDesc = NaturalDesc(_sortMode);
        UpdateSortDirUi();
        if (!_ready) return;
        if (_viewMode is "songs" or "favorites" or "history" or "playlist" or "album" or "artist")
        {
            RefreshTracks();
        }
    }

    private void ApplyFilter()
    {
        if (!_ready) return;
        switch (_viewMode)
        {
            case "albums":
                FilterAlbums();
                break;
            case "artists":
                FilterArtists();
                break;
            case "folders":
            case "genres":
                FilterCategories();
                break;
            case "home":
                break;
            default:
                RefreshTracks();
                break;
        }
    }

    private void RefreshTracks()
    {
        var term = SearchBox.Text.Trim();
        IEnumerable<Track> seq = _base;
        if (term.Length > 0)
        {
            seq = seq.Where(t => Matches(t, term));
        }
        seq = SortTracks(seq);
        if (_sortDesc) seq = seq.Reverse();

        var list = seq.ToList();
        var n = 0;
        foreach (var t in list) t.RowNumber = ++n;

        _view = list;
        TrackList.ItemsSource = list;
        TrackEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        TrackEmptyText.Text = term.Length > 0 ? $"没有找到“{term}”" : "这里还没有歌曲";

        ViewSub.Text = term.Length > 0
            ? $"{_subPrefix}{list.Count:N0} / {_base.Count:N0} 首"
            : $"{_subPrefix}{list.Count:N0} 首";
        UpdateAzRail();
    }

    private IEnumerable<Track> SortTracks(IEnumerable<Track> seq) => _sortMode switch
    {
        "title" => seq.OrderBy(t => t.TitleText, StringComparer.CurrentCultureIgnoreCase),
        "artist" => seq.OrderBy(t => t.ArtistText, StringComparer.CurrentCultureIgnoreCase)
                        .ThenBy(t => t.AlbumText, StringComparer.CurrentCultureIgnoreCase),
        "album" => seq.OrderBy(t => t.AlbumText, StringComparer.CurrentCultureIgnoreCase)
                       .ThenBy(t => t.DiscNo ?? 0).ThenBy(t => t.TrackNo ?? 0),
        "duration" => seq.OrderBy(t => t.Duration ?? 0),
        "year" => seq.OrderBy(t => t.Year ?? 0)
                       .ThenBy(t => t.AlbumText, StringComparer.CurrentCultureIgnoreCase)
                       .ThenBy(t => t.DiscNo ?? 0).ThenBy(t => t.TrackNo ?? 0),
        "added" => seq.OrderBy(t => t.AddedAt ?? 0),
        "size" => seq.OrderBy(t => t.Size ?? 0),
        "bitrate" => seq.OrderBy(t => t.Bitrate ?? 0),
        _ => seq,
    };

    private static bool NaturalDesc(string mode) =>
        mode is "duration" or "year" or "added" or "size" or "bitrate";

    private void UpdateSortDirUi()
    {
        // XAML 解析时 SortBox 的初始选中项会先触发 SelectionChanged，
        // 此时后面声明的 SortDirGlyph 还没实例化。
        if (SortDirGlyph is null) return;
        SortDirGlyph.Glyph = _sortDesc ? "\uE70D" : "\uE70E";
        ToolTipService.SetToolTip(SortDirButton, _sortDesc ? "当前降序，点击改为升序" : "当前升序，点击改为降序");
    }

    private void SortDir_Click(object sender, RoutedEventArgs e)
    {
        _sortDesc = !_sortDesc;
        UpdateSortDirUi();
        if (_ready && _viewMode is "songs" or "favorites" or "history" or "playlist" or "album" or "artist")
        {
            RefreshTracks();
        }
    }

    private static bool Matches(Track t, string term) =>
        t.TitleText.Contains(term, StringComparison.CurrentCultureIgnoreCase) ||
        t.ArtistText.Contains(term, StringComparison.CurrentCultureIgnoreCase) ||
        t.AlbumText.Contains(term, StringComparison.CurrentCultureIgnoreCase) ||
        (t.AlbumArtist?.Contains(term, StringComparison.CurrentCultureIgnoreCase) ?? false) ||
        (t.Genre?.Contains(term, StringComparison.CurrentCultureIgnoreCase) ?? false);

    private void FilterAlbums()
    {
        var term = SearchBox.Text.Trim();
        var list = term.Length == 0
            ? _albumsAll.ToList()
            : _albumsAll.Where(a =>
                a.Album.Contains(term, StringComparison.CurrentCultureIgnoreCase) ||
                a.ArtistText.Contains(term, StringComparison.CurrentCultureIgnoreCase)).ToList();
        AlbumGrid.ItemsSource = list;
        AlbumsEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        AlbumsEmptyText.Text = term.Length > 0 ? $"没有找到“{term}”" : "这里还没有专辑";
        ViewSub.Text = term.Length > 0
            ? $"{list.Count:N0} / {_albumsAll.Count:N0} 张专辑"
            : $"{list.Count:N0} 张专辑";
    }

    private void FilterArtists()
    {
        var term = SearchBox.Text.Trim();
        var list = term.Length == 0
            ? _artistsAll.ToList()
            : _artistsAll.Where(a => a.Name.Contains(term, StringComparison.CurrentCultureIgnoreCase)).ToList();
        ArtistGrid.ItemsSource = list;
        ArtistsEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        ArtistsEmptyText.Text = term.Length > 0 ? $"没有找到“{term}”" : "这里还没有艺术家";
        ViewSub.Text = term.Length > 0
            ? $"{list.Count:N0} / {_artistsAll.Count:N0} 位艺术家"
            : $"{list.Count:N0} 位艺术家";
    }

    private async void TrackList_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (_player is null || e.ClickedItem is not Track track) return;
        var index = _view.IndexOf(track);
        if (index < 0) return;
        await _player.PlayFromAsync(_view, index, _context);
    }

    private async void QueueList_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (_player is null || e.ClickedItem is not Track track) return;
        var index = _player.Queue.IndexOf(track);
        if (index < 0) return;
        QueueFlyout.Hide();
        await _player.PlayAtAsync(index);
    }

    private async void QueueClear_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null || _player.Queue.Count == 0) return;
        await _player.ClearQueueAsync(keepCurrent: true);
        ShowToast("已清空队列，保留当前曲目");
    }

    // ---------------------------------------------------------- track context menu

    private MenuFlyout? _trackMenu;
    private Track? _menuTrack;
    private bool _menuFromQueue;
    private long _playlistId;
    private MenuFlyoutItem? _miNext, _miEnqueue, _miFav, _miPlRemove, _miQRemove, _miUp, _miDown;
    private MenuFlyoutSubItem? _miPlaylist;

    private void EnsureTrackMenu()
    {
        if (_trackMenu is not null) return;
        _trackMenu = new MenuFlyout
        {
            MenuFlyoutPresenterStyle = GlassMenuPresenterStyle(),
        };
        var items = _trackMenu.Items;
        items.Add(TrackMenuItem("播放", "\uE768", "play"));
        _miNext = TrackMenuItem("下一首播放", "\uE893", "next");
        _miEnqueue = TrackMenuItem("加入队列", "\uE8FD", "enqueue");
        items.Add(_miNext);
        items.Add(_miEnqueue);
        _miPlaylist = new MenuFlyoutSubItem
        {
            Text = "添加到歌单",
            Icon = new FontIcon { Glyph = "\uE710", FontSize = 14 },
        };
        items.Add(_miPlaylist);
        items.Add(new MenuFlyoutSeparator());
        _miFav = TrackMenuItem("收藏", "\uE734", "fav");
        items.Add(_miFav);
        items.Add(new MenuFlyoutSeparator());
        items.Add(TrackMenuItem("曲目详情", "\uE946", "info"));
        items.Add(TrackMenuItem("复制文件路径", "\uE8C8", "copy"));
        items.Add(TrackMenuItem("在资源管理器中显示", "\uE8B7", "reveal"));
        _miUp = TrackMenuItem("上移一位", "\uE70E", "pl_up");
        _miDown = TrackMenuItem("下移一位", "\uE70D", "pl_down");
        _miPlRemove = TrackMenuItem("从歌单中移除", "\uE74D", "pl_remove");
        _miQRemove = TrackMenuItem("从队列中移除", "\uE74D", "q_remove");
        items.Add(_miUp);
        items.Add(_miDown);
        items.Add(_miPlRemove);
        items.Add(_miQRemove);
    }

    private static Style GlassMenuPresenterStyle()
    {
        var style = new Style(typeof(MenuFlyoutPresenter));
        style.Setters.Add(new Setter(MenuFlyoutPresenter.BackgroundProperty, Application.Current.Resources["GlassRaisedBrush"]));
        style.Setters.Add(new Setter(MenuFlyoutPresenter.BorderBrushProperty, Application.Current.Resources["HairlineStrongBrush"]));
        style.Setters.Add(new Setter(MenuFlyoutPresenter.BorderThicknessProperty, new Thickness(1)));
        style.Setters.Add(new Setter(MenuFlyoutPresenter.CornerRadiusProperty, new CornerRadius(12)));
        return style;
    }

    private MenuFlyoutItem TrackMenuItem(string text, string glyph, string tag)
    {
        var item = new MenuFlyoutItem
        {
            Text = text,
            Tag = tag,
            Icon = new FontIcon { Glyph = glyph, FontSize = 14 },
        };
        item.Click += TrackMenu_Click;
        return item;
    }

    private void TrackRow_RightTapped(object sender, RightTappedRoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: Track track }) return;
        e.Handled = true;
        OpenTrackMenu(track, (FrameworkElement)sender, e.GetPosition((UIElement)sender), fromQueue: false);
    }

    private void QueueRow_RightTapped(object sender, RightTappedRoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: Track track }) return;
        e.Handled = true;
        OpenTrackMenu(track, (FrameworkElement)sender, e.GetPosition((UIElement)sender), fromQueue: true);
    }

    private void OpenTrackMenu(Track track, FrameworkElement anchor, Point pos, bool fromQueue)
    {
        if (_player is null || !_ready) return;
        EnsureTrackMenu();
        _menuTrack = track;
        _menuFromQueue = fromQueue;
        _miNext!.Visibility = fromQueue ? Visibility.Collapsed : Visibility.Visible;
        _miEnqueue!.Visibility = fromQueue ? Visibility.Collapsed : Visibility.Visible;
        _miFav!.Text = track.Favorite ? "取消收藏" : "收藏";
        _miFav.Icon = new FontIcon { Glyph = track.Favorite ? "\uE735" : "\uE734", FontSize = 14 };
        _miPlRemove!.Visibility = !fromQueue && _viewMode == "playlist" ? Visibility.Visible : Visibility.Collapsed;
        _miQRemove!.Visibility = fromQueue ? Visibility.Visible : Visibility.Collapsed;
        var playlistView = !fromQueue && _viewMode == "playlist";
        _miUp!.Visibility = playlistView ? Visibility.Visible : Visibility.Collapsed;
        _miDown!.Visibility = playlistView ? Visibility.Visible : Visibility.Collapsed;
        _ = FillPlaylistSubmenuAsync(track);
        _trackMenu!.ShowAt(anchor, new FlyoutShowOptions { Position = pos });
    }

    private async Task FillPlaylistSubmenuAsync(Track track)
    {
        var sub = _miPlaylist!;
        sub.Items.Clear();
        List<PlaylistInfo> playlists;
        try
        {
            playlists = (await _api!.GetAsync<PlaylistsResponse>("playlists")).Items;
            if (_boot is not null) _boot.Playlists = playlists;
            PlaylistNav.ItemsSource = playlists;
        }
        catch
        {
            playlists = _boot?.Playlists ?? new List<PlaylistInfo>();
        }
        foreach (var pl in playlists)
        {
            var item = new MenuFlyoutItem { Text = pl.Name, Tag = pl.Id };
            item.Click += async (_, _) => await AddTrackToPlaylistAsync(track, pl);
            sub.Items.Add(item);
        }
        if (sub.Items.Count > 0) sub.Items.Add(new MenuFlyoutSeparator());
        var create = new MenuFlyoutItem
        {
            Text = "新建歌单…",
            Icon = new FontIcon { Glyph = "\uE710", FontSize = 14 },
        };
        create.Click += async (_, _) => await CreatePlaylistWithTrackAsync(track);
        sub.Items.Add(create);
    }

    private async Task RefreshPlaylistsAsync()
    {
        var r = await _api!.GetAsync<PlaylistsResponse>("playlists");
        if (_boot is not null) _boot.Playlists = r.Items;
        PlaylistNav.ItemsSource = r.Items;
    }

    private async Task AddTrackToPlaylistAsync(Track track, PlaylistInfo pl)
    {
        try
        {
            await _api!.PostAsync($"playlists/{pl.Id}/add", new { track_id = track.Id });
            ShowToast($"已添加到「{pl.Name}」");
            await RefreshPlaylistsAsync();
            if (_viewMode == "playlist" && _playlistId == pl.Id) await ShowPlaylistAsync(pl);
        }
        catch (Exception ex) { ShowToast("添加失败：" + ex.Message); }
    }

    private async Task CreatePlaylistWithTrackAsync(Track track)
    {
        var name = await PromptTextAsync("新建歌单", $"把「{track.TitleText}」放进新歌单", "", "创建");
        if (name is null) return;
        try
        {
            var r = await _api!.PostAsync<PlaylistsResponse>("playlists", new { name });
            await _api.PostAsync($"playlists/{r.Id}/add", new { track_id = track.Id });
            await RefreshPlaylistsAsync();
            ShowToast($"已创建「{name}」并添加曲目");
        }
        catch (Exception ex) { ShowToast("创建失败：" + ex.Message); }
    }

    private async void TrackMenu_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null || _menuTrack is not { } track) return;
        var tag = (sender as MenuFlyoutItem)?.Tag as string;
        try
        {
            switch (tag)
            {
                case "play":
                    if (_menuFromQueue)
                    {
                        var qi = _player.Queue.IndexOf(track);
                        if (qi >= 0)
                        {
                            QueueFlyout.Hide();
                            await _player.PlayAtAsync(qi);
                        }
                    }
                    else
                    {
                        var vi = _view.IndexOf(track);
                        if (vi >= 0) await _player.PlayFromAsync(_view, vi, _context);
                        else await _player.PlayFromAsync(new[] { track }, 0, track.AlbumText);
                    }
                    break;
                case "next":
                    await _player.PlayNextAsync(track);
                    ShowToast("已插队，下一首播放");
                    break;
                case "enqueue":
                    await _player.EnqueueAsync(track);
                    ShowToast("已加入播放队列");
                    break;
                case "fav":
                    await _player.ToggleFavoriteAsync(track);
                    break;
                case "info":
                    await ShowTrackInfoAsync(track);
                    break;
                case "copy":
                {
                    var full = await FullTrackAsync(track);
                    if (full.Path is { Length: > 0 } p)
                    {
                        var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
                        dp.SetText(p);
                        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
                        ShowToast("已复制文件路径");
                    }
                    break;
                }
                case "reveal":
                {
                    var full = await FullTrackAsync(track);
                    if (full.Path is { Length: > 0 } p)
                    {
                        var r = await _api!.PostAsync<OkResponse>("window/reveal", new { path = p });
                        if (!r.Ok) ShowToast(r.Error ?? "无法打开资源管理器");
                    }
                    break;
                }
                case "pl_remove":
                    await _api!.PostAsync($"playlists/{_playlistId}/remove", new { track_id = track.Id });
                    _base.RemoveAll(t => t.Id == track.Id);
                    RefreshTracks();
                    await RefreshPlaylistsAsync();
                    ShowToast("已从歌单移除");
                    break;
                case "pl_up":
                case "pl_down":
                {
                    var idx = _base.FindIndex(t => t.Id == track.Id);
                    if (idx < 0) break;
                    var target = tag == "pl_up" ? idx - 1 : idx + 1;
                    if (target < 0 || target >= _base.Count) break;
                    await _api!.PostAsync($"playlists/{_playlistId}/move",
                        new { track_id = track.Id, position = target });
                    var item = _base[idx];
                    _base.RemoveAt(idx);
                    _base.Insert(target, item);
                    RefreshTracks();
                    break;
                }
                case "q_remove":
                {
                    var qi = _player.Queue.IndexOf(track);
                    if (qi >= 0) await _player.RemoveAtAsync(qi);
                    break;
                }
            }
        }
        catch (Exception ex) { ShowToast("操作失败：" + ex.Message); }
    }

    private async Task<Track> FullTrackAsync(Track track)
    {
        if (track.Path is { Length: > 0 }) return track;
        try
        {
            var r = await _api!.GetAsync<TrackResponse>($"track/{track.Id}");
            if (r.Track is { } full)
            {
                track.Path = full.Path;
                track.BitDepth = full.BitDepth;
                return track;
            }
        }
        catch { }
        return track;
    }

    // ---------------------------------------------------------- dialogs

    private ContentDialog NewDialog(string title, string primary, string close = "取消")
    {
        return new ContentDialog
        {
            Title = title,
            PrimaryButtonText = primary,
            CloseButtonText = close,
            DefaultButton = ContentDialogButton.Primary,
            XamlRoot = Content.XamlRoot,
            Background = (Brush)Application.Current.Resources["GlassRaisedBrush"],
            BorderBrush = (Brush)Application.Current.Resources["HairlineStrongBrush"],
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(16),
        };
    }

    private async Task<string?> PromptTextAsync(string title, string subtitle, string initial, string okLabel)
    {
        var input = new TextBox
        {
            Text = initial,
            PlaceholderText = "名称",
            SelectionStart = initial.Length,
            CornerRadius = new CornerRadius(8),
        };
        var panel = new StackPanel { Spacing = 10 };
        if (subtitle.Length > 0)
        {
            panel.Children.Add(new TextBlock
            {
                Text = subtitle,
                TextWrapping = TextWrapping.Wrap,
                Style = (Style)Application.Current.Resources["CaptionText"],
            });
        }
        panel.Children.Add(input);
        var dialog = NewDialog(title, okLabel);
        dialog.Content = panel;
        string? result = null;
        dialog.PrimaryButtonClick += (_, args) =>
        {
            var text = input.Text.Trim();
            if (text.Length == 0)
            {
                args.Cancel = true;
                return;
            }
            result = text;
        };
        await dialog.ShowAsync();
        return result;
    }

    private async Task ShowTrackInfoAsync(Track track)
    {
        var full = await FullTrackAsync(track);
        var rows = new List<(string Label, string Value)>
        {
            ("标题", full.TitleText),
            ("艺术家", full.ArtistText),
            ("专辑", full.AlbumText),
        };
        if (!string.IsNullOrWhiteSpace(full.AlbumArtist)) rows.Add(("专辑艺术家", full.AlbumArtist!));
        if (!string.IsNullOrWhiteSpace(full.Genre)) rows.Add(("流派", full.Genre!));
        if (full.Year is > 0) rows.Add(("年份", full.Year!.Value.ToString()));
        if (full.TrackNo is > 0)
            rows.Add(("音轨", full.DiscNo is > 0 ? $"{full.TrackNo} · 碟 {full.DiscNo}" : full.TrackNo!.Value.ToString()));
        rows.Add(("时长", full.DurationText));
        var spec = string.Join(" · ", new[]
        {
            (full.Fmt ?? "").ToUpperInvariant(),
            full.SampleRate is > 0 ? $"{full.SampleRate.Value / 1000.0:0.#} kHz" : "",
            full.BitDepth is > 0 ? $"{full.BitDepth} bit" : "",
            full.Bitrate is > 0 ? $"{full.Bitrate} kbps" : "",
            full.Channels is > 0 ? $"{full.Channels} 声道" : "",
        }.Where(s => s.Length > 0));
        if (spec.Length > 0) rows.Add(("规格", spec));
        if (full.Size is > 0) rows.Add(("大小", FormatBytes(full.Size.Value)));
        if (full.AddedAt is > 0)
            rows.Add(("添加时间", DateTimeOffset.FromUnixTimeSeconds((long)full.AddedAt.Value).LocalDateTime.ToString("yyyy-MM-dd HH:mm")));
        if (full.Path is { Length: > 0 }) rows.Add(("路径", full.Path!));

        var grid = new Grid { ColumnSpacing = 16, RowSpacing = 7, MaxWidth = 460 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        var caption = (Style)Application.Current.Resources["CaptionText"];
        for (var i = 0; i < rows.Count; i++)
        {
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var label = new TextBlock { Text = rows[i].Label, Style = caption, VerticalAlignment = VerticalAlignment.Top };
            Grid.SetRow(label, i);
            grid.Children.Add(label);
            var value = new TextBlock
            {
                Text = rows[i].Value,
                FontSize = 12.5,
                TextWrapping = TextWrapping.Wrap,
                IsTextSelectionEnabled = true,
                Foreground = _textPrimaryBrush,
            };
            Grid.SetRow(value, i);
            Grid.SetColumn(value, 1);
            grid.Children.Add(value);
        }
        var dialog = NewDialog("曲目详情", "关闭");
        dialog.CloseButtonText = null;
        dialog.Content = grid;
        await dialog.ShowAsync();
    }

    private static string FormatBytes(long bytes)
    {
        if (bytes >= 1L << 30) return $"{bytes / (double)(1L << 30):0.##} GB";
        if (bytes >= 1L << 20) return $"{bytes / (double)(1L << 20):0.#} MB";
        return $"{bytes / 1024.0:0} KB";
    }

    // ---------------------------------------------------------- playlists

    private MenuFlyout? _playlistMenu;
    private PlaylistInfo? _menuPlaylist;
    private MenuFlyoutItem? _pmiPin;

    private void EnsurePlaylistMenu()
    {
        if (_playlistMenu is not null) return;
        _playlistMenu = new MenuFlyout { MenuFlyoutPresenterStyle = GlassMenuPresenterStyle() };
        var items = _playlistMenu.Items;
        items.Add(PlaylistMenuItem("播放", "\uE768", "play"));
        items.Add(PlaylistMenuItem("重命名…", "\uE8AC", "rename"));
        _pmiPin = PlaylistMenuItem("置顶", "\uE840", "pin");
        items.Add(_pmiPin);
        items.Add(new MenuFlyoutSeparator());
        items.Add(PlaylistMenuItem("导出为 M3U…", "\uE896", "export"));
        items.Add(new MenuFlyoutSeparator());
        items.Add(PlaylistMenuItem("删除歌单", "\uE74D", "delete"));
    }

    private MenuFlyoutItem PlaylistMenuItem(string text, string glyph, string tag)
    {
        var item = new MenuFlyoutItem
        {
            Text = text,
            Tag = tag,
            Icon = new FontIcon { Glyph = glyph, FontSize = 14 },
        };
        item.Click += PlaylistMenu_Click;
        return item;
    }

    private void PlaylistNav_RightTapped(object sender, RightTappedRoutedEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: PlaylistInfo pl }) return;
        e.Handled = true;
        EnsurePlaylistMenu();
        _menuPlaylist = pl;
        _pmiPin!.Text = pl.Pinned != 0 ? "取消置顶" : "置顶";
        _playlistMenu!.ShowAt((FrameworkElement)sender, new FlyoutShowOptions { Position = e.GetPosition((UIElement)sender) });
    }

    private async void PlaylistMenu_Click(object sender, RoutedEventArgs e)
    {
        if (_menuPlaylist is not { } pl) return;
        var tag = (sender as MenuFlyoutItem)?.Tag as string;
        try
        {
            switch (tag)
            {
                case "play":
                    await PlayPlaylistAsync(pl);
                    break;
                case "rename":
                    await RenamePlaylistAsync(pl);
                    break;
                case "pin":
                    await _api!.PostAsync<PlaylistsResponse>("playlists/pin", new { id = pl.Id, pinned = pl.Pinned == 0 });
                    await RefreshPlaylistsAsync();
                    ShowToast(pl.Pinned != 0 ? $"已取消置顶「{pl.Name}」" : $"已置顶「{pl.Name}」");
                    break;
                case "export":
                    await ExportPlaylistAsync(pl);
                    break;
                case "delete":
                    await DeletePlaylistAsync(pl);
                    break;
            }
        }
        catch (Exception ex) { ShowToast("操作失败：" + ex.Message); }
    }

    private async void PlaylistAdd_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        var name = await PromptTextAsync("新建歌单", "", "", "创建");
        if (name is null) return;
        try
        {
            await _api!.PostAsync<PlaylistsResponse>("playlists", new { name });
            await RefreshPlaylistsAsync();
            ShowToast($"已创建「{name}」");
        }
        catch (Exception ex) { ShowToast("创建失败：" + ex.Message); }
    }

    private async Task PlayPlaylistAsync(PlaylistInfo pl)
    {
        var r = await _api!.GetAsync<TracksResponse>($"playlist/{pl.Id}?limit=5000");
        if (r.Items.Count == 0)
        {
            ShowToast("这个歌单还是空的");
            return;
        }
        var list = r.Items.Select(Unify).ToList();
        foreach (var t in list) PrepareTrack(t);
        await _player!.PlayFromAsync(list, 0, pl.Name);
    }

    private async Task RenamePlaylistAsync(PlaylistInfo pl)
    {
        var name = await PromptTextAsync("重命名歌单", $"原名：{pl.Name}", pl.Name, "保存");
        if (name is null || name == pl.Name) return;
        await _api!.PostAsync("playlists/rename", new { id = pl.Id, name });
        await RefreshPlaylistsAsync();
        if (_viewMode == "playlist" && _playlistId == pl.Id)
        {
            await ShowPlaylistAsync(new PlaylistInfo { Id = pl.Id, Name = name, CoverTrack = pl.CoverTrack });
        }
        ShowToast($"已重命名为「{name}」");
    }

    private async Task ExportPlaylistAsync(PlaylistInfo pl)
    {
        var r = await _api!.PostAsync<M3uExportResponse>("playlists/export", new { id = pl.Id });
        if (!r.Ok || string.IsNullOrEmpty(r.Path))
        {
            ShowToast(r.Error ?? "导出失败");
            return;
        }
        var path = r.Path!;
        var extra = r.Missing > 0 ? $"，{r.Missing} 首文件已失效" : "";
        ShowToast($"已导出「{pl.Name}」：{r.Count} 首{extra}", "在文件夹中显示", async () =>
        {
            try
            {
                var rr = await _api!.PostAsync<OkResponse>("window/reveal", new { path });
                if (!rr.Ok) ShowToast(rr.Error ?? "无法打开资源管理器");
            }
            catch (Exception ex) { ShowToast("打开失败：" + ex.Message); }
        });
    }

    private async Task DeletePlaylistAsync(PlaylistInfo pl)
    {
        var tracks = await _api!.GetAsync<TracksResponse>($"playlist/{pl.Id}?limit=5000");
        var snap = new
        {
            id = pl.Id,
            name = pl.Name,
            sort_order = pl.SortOrder,
            pinned = pl.Pinned,
            track_ids = tracks.Items.Select(t => t.Id).ToArray(),
        };
        await _api.PostAsync("playlists/delete", new { id = pl.Id });
        await RefreshPlaylistsAsync();
        var wasViewing = _viewMode == "playlist" && _playlistId == pl.Id;
        if (wasViewing) await GoToAsync(_backTarget);
        ShowToast($"已删除「{pl.Name}」", "撤销", async () =>
        {
            try
            {
                await _api!.PostAsync<PlaylistsResponse>("playlists/restore", snap);
                await RefreshPlaylistsAsync();
                ShowToast($"已恢复「{pl.Name}」");
            }
            catch (Exception ex) { ShowToast("恢复失败：" + ex.Message); }
        });
    }

    // ---------------------------------------------------------- keyboard + sleep timer

    private readonly DispatcherTimer _sleepTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private DateTime _sleepDeadline;
    private bool _sleepAfterTrack;
    private long _sleepTrackId;
    private int _sleepTicks;

    private void SleepTimer_Tick(object? sender, object e)
    {
        if (_player is null)
        {
            _sleepTimer.Stop();
            return;
        }
        if (_sleepAfterTrack)
        {
            if (_player.Current is { } cur && cur.Id != _sleepTrackId)
            {
                _sleepTimer.Stop();
                _sleepAfterTrack = false;
                SleepTimerButton.Foreground = _dimBrush;
                SleepCancelItem.Visibility = Visibility.Collapsed;
                SleepCancelSep.Visibility = Visibility.Collapsed;
                ToolTipService.SetToolTip(SleepTimerButton, "定时关闭");
                if (_player.IsPlaying) _ = _player.ToggleAsync();
                ShowToast("定时关闭：当前曲目已播完，已暂停");
            }
            return;
        }
        var left = _sleepDeadline - DateTime.UtcNow;
        if (left <= TimeSpan.Zero)
        {
            _sleepTimer.Stop();
            SleepTimerButton.Foreground = _dimBrush;
            SleepCancelItem.Visibility = Visibility.Collapsed;
            SleepCancelSep.Visibility = Visibility.Collapsed;
            ToolTipService.SetToolTip(SleepTimerButton, "定时关闭");
            if (_player.IsPlaying) _ = _player.ToggleAsync();
            ShowToast("定时关闭：已暂停播放");
            return;
        }
        if (++_sleepTicks % 15 == 0)
        {
            var ts = TimeSpan.FromSeconds(Math.Ceiling(left.TotalSeconds));
            var text = ts.TotalHours >= 1 ? $"{(int)ts.TotalHours}:{ts.Minutes:00}:{ts.Seconds:00}" : $"{ts.Minutes}:{ts.Seconds:00}";
            ToolTipService.SetToolTip(SleepTimerButton, $"定时关闭：还剩 {text}");
        }
    }

    private void SleepTimer_Click(object sender, RoutedEventArgs e)
    {
        var tag = (sender as MenuFlyoutItem)?.Tag as string ?? "";
        if (tag == "track")
        {
            if (_player?.Current is null)
            {
                ShowToast("先播放一首歌，再设置播完停止");
                return;
            }
            _sleepAfterTrack = true;
            _sleepTrackId = _player.Current.Id;
            _sleepTicks = 0;
            _sleepTimer.Start();
            SleepCancelItem.Visibility = Visibility.Visible;
            SleepCancelSep.Visibility = Visibility.Visible;
            SleepTimerButton.Foreground = _accentBrush;
            ToolTipService.SetToolTip(SleepTimerButton, "定时关闭：播完当前曲目后暂停");
            ShowToast("定时关闭：播完当前曲目后暂停");
            return;
        }
        if (!int.TryParse(tag, out var minutes)) return;
        _sleepAfterTrack = false;
        _sleepDeadline = DateTime.UtcNow.AddMinutes(minutes);
        _sleepTicks = 0;
        _sleepTimer.Start();
        SleepCancelItem.Visibility = Visibility.Visible;
        SleepCancelSep.Visibility = Visibility.Visible;
        SleepTimerButton.Foreground = _accentBrush;
        ToolTipService.SetToolTip(SleepTimerButton, $"定时关闭：{minutes} 分钟后暂停");
        ShowToast($"定时关闭：{minutes} 分钟后暂停");
    }

    private void SleepCancel_Click(object sender, RoutedEventArgs e)
    {
        _sleepTimer.Stop();
        _sleepAfterTrack = false;
        SleepCancelItem.Visibility = Visibility.Collapsed;
        SleepCancelSep.Visibility = Visibility.Collapsed;
        SleepTimerButton.Foreground = _dimBrush;
        ToolTipService.SetToolTip(SleepTimerButton, "定时关闭");
        ShowToast("已取消定时关闭");
    }

    private void Root_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (!_ready || _player is null) return;
        var focused = FocusManager.GetFocusedElement(Content.XamlRoot);
        if (focused is TextBox or PasswordBox or RichEditBox or AutoSuggestBox) return;
        var ctrl = Microsoft.UI.Input.InputKeyboardSource
            .GetKeyStateForCurrentThread(Windows.System.VirtualKey.Control)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (ctrl) return;

        switch (e.Key)
        {
            case Windows.System.VirtualKey.Space:
                if (focused is ButtonBase or CheckBox or RadioButton or ComboBox or ToggleSwitch) return;
                e.Handled = true;
                _ = _player.ToggleAsync();
                break;
            case Windows.System.VirtualKey.Left:
                if (focused is Slider) return;
                e.Handled = true;
                _ = _player.SeekAsync(Math.Max(0, _player.Position - 5));
                break;
            case Windows.System.VirtualKey.Right:
                if (focused is Slider) return;
                e.Handled = true;
                _ = _player.SeekAsync(Math.Min(_player.Duration, _player.Position + 5));
                break;
            case Windows.System.VirtualKey.Up:
                if (focused is Slider) return;
                e.Handled = true;
                _player.SetVolume(_player.Volume + 0.05);
                break;
            case Windows.System.VirtualKey.Down:
                if (focused is Slider) return;
                e.Handled = true;
                _player.SetVolume(_player.Volume - 0.05);
                break;
            case Windows.System.VirtualKey.PageDown:
            case Windows.System.VirtualKey.N:
                e.Handled = true;
                _ = _player.NextAsync();
                break;
            case Windows.System.VirtualKey.PageUp:
            case Windows.System.VirtualKey.P:
                e.Handled = true;
                _ = _player.PreviousAsync();
                break;
            case Windows.System.VirtualKey.M:
                e.Handled = true;
                _ = CyclePlayModeAsync();
                break;
            case Windows.System.VirtualKey.L:
                e.Handled = true;
                Lyrics_Click(this, new RoutedEventArgs());
                break;
            case Windows.System.VirtualKey.Q:
                e.Handled = true;
                QueueButton.Flyout?.ShowAt(QueueButton);
                break;
            case Windows.System.VirtualKey.I:
                if (_player.Current is { } current)
                {
                    e.Handled = true;
                    _ = ShowTrackInfoAsync(current);
                }
                break;
            case Windows.System.VirtualKey.Delete:
                e.Handled = true;
                _ = _player.ClearQueueAsync(keepCurrent: true);
                break;
            case Windows.System.VirtualKey.F1:
                e.Handled = true;
                _ = ShowShortcutsAsync();
                break;
            default:
                // OEM 键位：0xBF 同时是 "/" 和 "?"，靠 Shift 区分。
                if ((int)e.Key == 0xBF)
                {
                    var shift = Microsoft.UI.Input.InputKeyboardSource
                        .GetKeyStateForCurrentThread(Windows.System.VirtualKey.Shift)
                        .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
                    e.Handled = true;
                    if (shift) _ = ShowShortcutsAsync();
                    else
                    {
                        SearchBox.Focus(FocusState.Keyboard);
                        SearchBox.SelectAll();
                    }
                }
                break;
        }
    }

    private async Task CyclePlayModeAsync()
    {
        if (_player is null) return;
        var next = _player.PlayMode switch
        {
            "list_loop" => "one_loop",
            "one_loop" => "shuffle",
            "shuffle" => "normal",
            _ => "list_loop",
        };
        await _player.SetPlayModeAsync(next);
        ShowToast(next switch
        {
            "list_loop" => "播放模式：列表循环",
            "one_loop" => "播放模式：单曲循环",
            "shuffle" => "播放模式：随机播放",
            _ => "播放模式：顺序播放",
        });
    }

    private async Task ShowShortcutsAsync()
    {
        var rows = new (string Desc, string Keys)[]
        {
            ("播放 / 暂停", "空格"),
            ("快退 / 快进 5 秒", "← / →"),
            ("音量 − / +", "↓ / ↑"),
            ("上一首 / 下一首", "P / N"),
            ("搜索", "/"),
            ("歌词 / 队列 / 曲目详情", "L / Q / I"),
            ("播放模式（列表/单曲/随机/顺序）", "M"),
            ("清空播放队列（保留当前曲目）", "Delete"),
            ("快捷键总览", "? 或 F1"),
            ("关闭歌词 / 浮层", "Esc"),
        };
        var grid = new Grid { ColumnSpacing = 24, RowSpacing = 7, MinWidth = 360 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        for (var i = 0; i < rows.Length; i++)
        {
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var desc = new TextBlock
            {
                Text = rows[i].Desc,
                FontSize = 12.5,
                Foreground = _textPrimaryBrush,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetRow(desc, i);
            grid.Children.Add(desc);
            var keys = new Border
            {
                Background = _controlFillBrush,
                BorderBrush = _controlStrokeBrush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(8, 3, 8, 3),
                Child = new TextBlock { Text = rows[i].Keys, FontSize = 11.5, Foreground = (Brush)Application.Current.Resources["TextSecondaryBrush"] },
            };
            Grid.SetRow(keys, i);
            Grid.SetColumn(keys, 1);
            grid.Children.Add(keys);
        }
        var dialog = NewDialog("键盘快捷键", "知道了");
        dialog.CloseButtonText = null;
        dialog.Content = grid;
        await dialog.ShowAsync();
    }

    private async void AlbumGrid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (!_ready || e.ClickedItem is not AlbumInfo album) return;
        try
        {
            await ShowAlbumAsync(album);
        }
        catch (Exception ex)
        {
            ShowToast("专辑加载失败：" + ex.Message);
        }
    }

    private async void ArtistGrid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (!_ready || e.ClickedItem is not ArtistInfo artist) return;
        try
        {
            await ShowArtistAsync(artist);
        }
        catch (Exception ex)
        {
            ShowToast("艺术家加载失败：" + ex.Message);
        }
    }

    private async void AlbumCard_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        if ((sender as FrameworkElement)?.DataContext is not AlbumInfo album) return;
        await ShowAlbumAsync(album);
    }

    private async void TrackCard_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        if ((sender as FrameworkElement)?.DataContext is not Track track) return;
        var list = _recent.Contains(track) ? _recent : _fresh.Contains(track) ? _fresh : _allTracks;
        var index = list.IndexOf(track);
        if (index < 0) return;
        await _player.PlayFromAsync(list, index, "首页");
    }

    private async void HistoryLink_Click(object sender, RoutedEventArgs e) => await GoToAsync("history");

    private async void SongsLink_Click(object sender, RoutedEventArgs e) => await GoToAsync("songs");

    private async void AlbumsLink_Click(object sender, RoutedEventArgs e) => await GoToAsync("albums");

    private async void Radio_Click(object sender, RoutedEventArgs e) => await GoToAsync("radio");

    private async void PlayAll_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null || _view.Count == 0) return;
        await _player.PlayFromAsync(_view, 0, _context);
    }

    private async void Shuffle_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null || _view.Count == 0) return;
        await _player.SetPlayModeAsync("shuffle");
        var shuffled = _view.OrderBy(_ => Random.Shared.Next()).ToList();
        await _player.PlayFromAsync(shuffled, 0, _context);
    }

    private async void HeroPlay_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        if (_player.Current is not null)
        {
            await _player.ToggleAsync();
            return;
        }
        var list = _view.Count > 0 ? _view : _allTracks;
        if (list.Count == 0) return;
        await _player.PlayFromAsync(list, 0, "所有歌曲");
    }

    // --------------------------------------------------------------- playback

    private async void PlayPause_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        if (_player.Current is null)
        {
            var list = _view.Count > 0 ? _view : _allTracks;
            if (list.Count > 0) await _player.PlayFromAsync(list, 0, _context);
            return;
        }
        await _player.ToggleAsync();
    }

    private async void Prev_Click(object sender, RoutedEventArgs e)
    {
        if (_player is not null) await _player.PreviousAsync();
    }

    private async void Next_Click(object sender, RoutedEventArgs e)
    {
        if (_player is not null) await _player.NextAsync();
    }

    private async void ShuffleToggle_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        await _player.SetPlayModeAsync(_player.PlayMode == "shuffle" ? "list_loop" : "shuffle");
        UpdateTransport();
    }

    private async void RepeatToggle_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        var next = _player.PlayMode switch
        {
            "list_loop" => "one_loop",
            "one_loop" => "normal",
            _ => "list_loop",
        };
        await _player.SetPlayModeAsync(next);
        UpdateTransport();
    }

    private void Mute_Click(object sender, RoutedEventArgs e)
    {
        _player?.ToggleMute();
    }

    private async void NowFavorite_Click(object sender, RoutedEventArgs e)
    {
        if (_player?.Current is null) return;
        var track = _player.Current;
        var was = track.Favorite;
        await _player.ToggleFavoriteAsync(track);
        if (track.Favorite == was) return;
        AdjustFavoriteCount(track.Favorite ? 1 : -1);
        SyncNowFavorite();
    }

    private async void RowFavorite_Click(object sender, RoutedEventArgs e)
    {
        if (_player is null) return;
        if ((sender as FrameworkElement)?.DataContext is not Track track) return;
        var was = track.Favorite;
        await _player.ToggleFavoriteAsync(track);
        if (track.Favorite == was) return;
        AdjustFavoriteCount(track.Favorite ? 1 : -1);
        if (_viewMode == "favorites" && !track.Favorite)
        {
            _base.Remove(track);
            RefreshTracks();
        }
        if (_player.Current?.Id == track.Id) SyncNowFavorite();
    }

    private void AdjustFavoriteCount(int delta)
    {
        _favCount = Math.Max(0, _favCount + delta);
        NavFavCount.Text = _favCount.ToString("N0");
    }

    private void VolumeSlider_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_syncingVolume || _player is null) return;
        _player.SetVolume(e.NewValue);
    }

    private void SeekSlider_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_syncingSeek) return;
        if (_seekDragging)
        {
            TimeNow.Text = FormatTime(e.NewValue);
            return;
        }
        if (_player is not null && _player.Current is not null && _player.Duration > 0)
        {
            _ = _player.SeekAsync(e.NewValue);
        }
    }

    private void SeekSlider_PointerPressed(object sender, PointerRoutedEventArgs e) => _seekDragging = true;

    private void SeekSlider_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_seekDragging) return;
        _seekDragging = false;
        if (_player is not null && _player.Current is not null)
        {
            _ = _player.SeekAsync(SeekSlider.Value);
        }
    }

    // ---------------------------------------------------------------- lyrics

    private void ApplyLyricSettings(SettingsDto s)
    {
        _lyricOffset = Math.Round(s.LyricOffset, 1);
        _showTranslation = s.ShowTranslation;
        _playbarLyric = s.PlaybarLyric;
        var size = s.LyricSize is > 40 and < 220 ? s.LyricSize : 100;
        _lyricScale = Math.Clamp(size / 100.0, 0.75, 1.6);
        LyricOffsetText.Text = FormatLyricOffset();
        UpdateTransButton();
        foreach (var line in _lyricLines)
        {
            line.TextSize = Math.Round(17 * _lyricScale, 1);
            line.ShowTrans = _showTranslation;
        }
        if (_lyricLines.Count > 0)
        {
            SetPlaybarLyric(_lyricIndex >= 0 && _lyricIndex < _lyricLines.Count
                ? _lyricLines[_lyricIndex].Text : null);
        }
    }

    private string FormatLyricOffset() =>
        Math.Abs(_lyricOffset) < 0.05 ? "0.0s" : $"{(_lyricOffset > 0 ? "+" : "")}{_lyricOffset:0.0}s";

    private void Lyrics_Click(object sender, RoutedEventArgs e)
    {
        if (_lyricOpen)
        {
            CloseLyrics();
            return;
        }
        if (_player?.Current is null)
        {
            ShowToast("先选一首歌，再打开歌词");
            return;
        }
        OpenLyrics();
    }

    private void LyricsClose_Click(object sender, RoutedEventArgs e) => CloseLyrics();

    private void Escape_Invoked(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs args)
    {
        if (!_lyricOpen) return;
        CloseLyrics();
        args.Handled = true;
    }

    private void OpenLyrics()
    {
        if (_lyricOpen) return;
        _lyricOpen = true;
        LyricsButton.Foreground = _accentBrush;
        LyricsButton.Background = _accentSoftBrush;
        ToolTipService.SetToolTip(LyricsButton, "关闭歌词");
        LyricsView.Visibility = Visibility.Visible;
        LyricsView.Opacity = 0;
        var fade = new DoubleAnimation
        {
            To = 1,
            Duration = TimeSpan.FromMilliseconds(170),
            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
        };
        Storyboard.SetTarget(fade, LyricsView);
        Storyboard.SetTargetProperty(fade, "Opacity");
        var sb = new Storyboard();
        sb.Children.Add(fade);
        sb.Begin();
        DispatcherQueue.TryEnqueue(() =>
        {
            _lyricScroll = FindScrollViewer(LyricsList);
            SyncLyric(scroll: true, forceScroll: true);
        });
        var track = _player?.Current;
        if (track is not null && _lyricTrackId != track.Id) _ = LoadLyricsAsync(track);
    }

    private void CloseLyrics()
    {
        if (!_lyricOpen) return;
        _lyricOpen = false;
        LyricsView.Visibility = Visibility.Collapsed;
        LyricsButton.Foreground = (Brush)Application.Current.Resources["TextSecondaryBrush"];
        LyricsButton.Background = _transparentBrush;
        ToolTipService.SetToolTip(LyricsButton, "歌词");
    }

    private async void LyricsTrans_Click(object sender, RoutedEventArgs e)
    {
        _showTranslation = !_showTranslation;
        foreach (var line in _lyricLines) line.ShowTrans = _showTranslation;
        UpdateTransButton();
        await SaveSettingsAsync(new Dictionary<string, object> { ["show_translation"] = _showTranslation });
    }

    private void UpdateTransButton()
    {
        LyricsTransButton.Background = _showTranslation ? _accentSoftBrush : _controlFillBrush;
        LyricsTransButton.BorderBrush = _showTranslation ? _accentBrush : _controlStrokeBrush;
        LyricsTransButton.Foreground = _showTranslation ? _accentBrush : _textPrimaryBrush;
        ToolTipService.SetToolTip(LyricsTransButton, _showTranslation ? "隐藏翻译" : "显示翻译");
    }

    private void LyricOffsetMinus_Click(object sender, RoutedEventArgs e) => NudgeLyricOffset(-0.1);

    private void LyricOffsetPlus_Click(object sender, RoutedEventArgs e) => NudgeLyricOffset(0.1);

    private async void LyricsAttach_Click(object sender, RoutedEventArgs e)
    {
        var track = _player?.Current;
        if (track is null || _api is null)
        {
            ShowToast("先播放一首歌，再挂载歌词");
            return;
        }
        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            picker.FileTypeFilter.Add(".lrc");
            picker.FileTypeFilter.Add(".txt");
            WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
            var file = await picker.PickSingleFileAsync();
            if (file is null) return;
            // 不把本机路径交给服务端读（审查 A2：任意文件读取）。WinUI 这边自己把文件的
            // 原始字节读出来、按 base64 上传，服务端按歌词编码（UTF-8/GBK/Big5…）解码后
            // 存进数据目录 —— 既不经手「读哪个路径」，也不丢非 UTF-8 歌词。
            var raw = await System.IO.File.ReadAllBytesAsync(file.Path);
            var r = await _api.PostAsync<LyricEditResponse>("lyrics/save_bytes",
                new { track_id = track.Id, data_b64 = Convert.ToBase64String(raw) });
            if (!r.Ok)
            {
                ShowToast(r.Error ?? "挂载失败");
                return;
            }
            track.HasLyric = 1;
            _lyricTrackId = 0;
            await LoadLyricsAsync(track);
            ShowToast($"已挂载歌词（{r.Lines} 行）");
        }
        catch (Exception ex) { ShowToast("挂载失败：" + ex.Message); }
    }

    private async void LyricsEdit_Click(object sender, RoutedEventArgs e)
    {
        var track = _player?.Current;
        if (track is null || _api is null)
        {
            ShowToast("先播放一首歌，再编辑歌词");
            return;
        }
        try
        {
            var ly = await _api.GetAsync<LyricsResponse>($"lyrics?track_id={track.Id}");
            var box = new TextBox
            {
                Text = LyricRawText(ly),
                AcceptsReturn = true,
                TextWrapping = TextWrapping.Wrap,
                Height = 360,
                MinWidth = 480,
                FontFamily = new FontFamily("Cascadia Mono, Consolas"),
                FontSize = 12.5,
                CornerRadius = new CornerRadius(10),
            };
            ScrollViewer.SetVerticalScrollBarVisibility(box, ScrollBarVisibility.Auto);
            var panel = new StackPanel { Spacing = 8 };
            panel.Children.Add(new TextBlock
            {
                Text = "支持 LRC 时间轴（[mm:ss.xx]歌词）。没有时间轴会按纯文本歌词显示。",
                Style = (Style)Application.Current.Resources["CaptionText"],
                TextWrapping = TextWrapping.Wrap,
            });
            panel.Children.Add(box);
            var dialog = NewDialog("编辑歌词", "保存");
            dialog.Content = panel;
            if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
            var r = await _api.PostAsync<LyricEditResponse>("lyrics/save",
                new { track_id = track.Id, text = box.Text });
            if (!r.Ok)
            {
                ShowToast(r.Error ?? "保存失败");
                return;
            }
            track.HasLyric = 1;
            _lyricTrackId = 0;
            await LoadLyricsAsync(track);
            ShowToast($"歌词已保存（{r.Lines} 行）");
        }
        catch (Exception ex) { ShowToast("保存失败：" + ex.Message); }
    }

    private static string LyricRawText(LyricsResponse r)
    {
        if (!string.IsNullOrWhiteSpace(r.Text)) return r.Text;
        if (r.Source is { Length: > 0 } src && File.Exists(src))
        {
            try { return File.ReadAllText(src); } catch { }
        }
        var sb = new System.Text.StringBuilder();
        foreach (var line in r.Lines)
        {
            var ts = TimeSpan.FromSeconds(Math.Max(0, line.T));
            var stamp = $"[{(int)ts.TotalMinutes:00}:{ts.Seconds:00}.{ts.Milliseconds / 10:00}]";
            sb.Append(stamp).Append(line.Text).Append('\n');
            if (!string.IsNullOrWhiteSpace(line.Trans)) sb.Append(stamp).Append(line.Trans).Append('\n');
        }
        return sb.ToString();
    }

    private void NudgeLyricOffset(double delta)
    {
        // 口径与 web/Qt 一致：正数 = 歌词整体往后推。
        _lyricOffset = Math.Round(Math.Clamp(_lyricOffset + delta, -10, 10), 1);
        LyricOffsetText.Text = FormatLyricOffset();
        SyncLyric(scroll: true, forceScroll: true);
        _ = SaveSettingsAsync(new Dictionary<string, object> { ["lyric_offset"] = _lyricOffset });
    }

    private async void LyricsList_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is not LyricLine line || _player?.Current is null || _lyricPlain) return;
        var index = _lyricLines.IndexOf(line);
        SetLyricIndex(index, scroll: true);
        await _player.SeekAsync(Math.Max(0, line.T + _lyricOffset));
        if (!_player.IsPlaying) await _player.ToggleAsync();
    }

    private async Task LoadLyricsAsync(Track? track)
    {
        var id = track?.Id ?? 0;
        if (id == _lyricTrackId)
        {
            SyncLyric(scroll: _lyricOpen, forceScroll: true);
            return;
        }
        _lyricTrackId = id;
        var version = ++_lyricVersion;
        _lyricLines.Clear();
        _lyricIndex = -1;
        _lyricPlain = false;
        LyricsList.ItemsSource = null;
        LyricsTitle.Text = track?.TitleText ?? "未在播放";
        LyricsArtist.Text = track is null ? "" : $"{track.ArtistText} · {track.AlbumText}";
        SetArt(LyricsArt, track, "art", 120);
        LyricsEmpty.Visibility = Visibility.Collapsed;
        LyricsList.Visibility = Visibility.Visible;
        LyricsRing.IsActive = false;
        if (track is null || _api is null)
        {
            ShowLyricsEmpty(true);
            return;
        }
        LyricsRing.IsActive = true;
        try
        {
            var r = await _api.GetAsync<LyricsResponse>($"lyrics?track_id={id}");
            if (version != _lyricVersion) return;
            if (r.Ok && r.Plain && !string.IsNullOrWhiteSpace(r.Text))
            {
                _lyricPlain = true;
                _lyricLines.Add(new LyricLine
                {
                    Text = r.Text.Trim(),
                    TextSize = Math.Round(16.5 * _lyricScale, 1),
                    ShowTrans = false,
                });
            }
            else if (r.Ok)
            {
                foreach (var line in r.Lines)
                {
                    if (string.IsNullOrWhiteSpace(line.Text)) continue;
                    line.TextSize = Math.Round(17 * _lyricScale, 1);
                    line.ShowTrans = _showTranslation;
                    _lyricLines.Add(line);
                }
            }
            Trace($"lyrics track={id} lines={_lyricLines.Count} plain={_lyricPlain} source={r.Source}");
            LyricsList.ItemsSource = _lyricLines;
            if (_lyricLines.Count == 0)
            {
                ShowLyricsEmpty(false);
                return;
            }
            DispatcherQueue.TryEnqueue(() =>
            {
                _lyricScroll = FindScrollViewer(LyricsList);
                SyncLyric(scroll: _lyricOpen);
            });
        }
        catch (Exception e)
        {
            if (version == _lyricVersion)
            {
                Trace("lyrics-error " + e.Message);
                ShowLyricsEmpty(false);
            }
        }
        finally
        {
            if (version == _lyricVersion) LyricsRing.IsActive = false;
        }
    }

    private void ShowLyricsEmpty(bool noTrack)
    {
        LyricsList.Visibility = Visibility.Collapsed;
        LyricsEmpty.Visibility = Visibility.Visible;
        LyricsEmptyHint.Text = noTrack
            ? "点一首歌开始播放，这里会显示歌词"
            : "把同名 .lrc 放在歌曲旁边，重启扫描后会自动挂载";
    }

    // scroll：换行时是否跟随（仅面板打开生效）。forceScroll：行号没变也强制重新居中，
    // 只给「打开面板 / 动偏移」这类显式重同步用；播放位置每拍都调 SyncLyric，绝不能带 force，
    // 否则同一行被反复 ChangeView 重启居中动画，画面会一直抖。
    private void SyncLyric(bool scroll = false, bool forceScroll = false)
    {
        if (_player is null) return;
        if (_lyricLines.Count == 0 || _lyricPlain)
        {
            SetPlaybarLyric(null);
            return;
        }
        var tt = _player.Position - _lyricOffset;
        var index = -1;
        for (var k = 0; k < _lyricLines.Count; k++)
        {
            if (_lyricLines[k].T <= tt + 0.12) index = k;
            else break;
        }
        if (index != _lyricIndex) SetLyricIndex(index, scroll);
        else if (forceScroll && _lyricOpen) ScrollLyricTo(index);
    }

    private void SetLyricIndex(int index, bool scroll)
    {
        _lyricIndex = index;
        for (var k = 0; k < _lyricLines.Count; k++)
        {
            var line = _lyricLines[k];
            line.Active = k == index;
            line.Past = k < index;
        }
        SetPlaybarLyric(index >= 0 && index < _lyricLines.Count ? _lyricLines[index].Text : null);
        if (scroll && _lyricOpen) ScrollLyricTo(index);
    }

    private void SetPlaybarLyric(string? text)
    {
        var raw = string.IsNullOrWhiteSpace(text) ? null : text!.Trim();
        var lyric = _playbarLyric ? raw : null;
        var want = lyric ?? _nowSub;
        if (NowArtist.Text != want) NowArtist.Text = want;
        ToolTipService.SetToolTip(NowArtist, lyric is null ? null : "歌词 · " + lyric);
        _playbarLyricText = raw ?? "";
        _mini?.SetLyric(raw);
    }

    private void ScrollLyricTo(int index)
    {
        if (_lyricLines.Count == 0) return;
        if (DateTime.UtcNow < _lyricHoldUntil) return;
        var target = Math.Clamp(index, 0, _lyricLines.Count - 1);
        var item = _lyricLines[target];
        _lyricScroll ??= FindScrollViewer(LyricsList);
        if (_lyricScroll is null) return;
        if (LyricsList.ContainerFromIndex(target) is not ListViewItem container)
        {
            // 目标行还没被虚拟化出来：先让它进入视口，下一拍再居中。
            LyricsList.ScrollIntoView(item, ScrollIntoViewAlignment.Leading);
            DispatcherQueue.TryEnqueue(() =>
            {
                if (LyricsList.ContainerFromIndex(target) is ListViewItem) ScrollLyricTo(target);
            });
            return;
        }
        try
        {
            if (_lyricScroll.Content is not UIElement content) return;
            var y = container.TransformToVisual(content).TransformPoint(new Point(0, 0)).Y;
            var offset = Math.Max(0, y - (_lyricScroll.ViewportHeight - container.ActualHeight) / 2);
            _lyricScroll.ChangeView(null, offset, null, disableAnimation: false);
        }
        catch
        {
            LyricsList.ScrollIntoView(item, ScrollIntoViewAlignment.Leading);
        }
    }

    private async Task SaveSettingsAsync(Dictionary<string, object> patch, bool configure = false)
    {
        if (_api is null) return;
        try
        {
            await _api.PostAsync("settings", new { settings = patch });
            if (configure)
            {
                await _api.PostAsync<NativeState>("native", new { action = "configure" });
            }
        }
        catch (Exception e)
        {
            ShowToast("设置保存失败：" + e.Message);
        }
    }

    // ------------------------------------------------------------- settings

    // 滑杆连续触发 ValueChanged，逐拍写盘/重开流既浪费又打架。统一进 350ms 防抖：
    // 一批改动合并成一次 POST；configure 只在有 DSP 改动时发；倍速这类开流参数改动后
    // 若正在播，落盘完从当前位置重开一条流（_pendingRestart）。
    private readonly Dictionary<string, object> _pendingPatch = new();
    private DispatcherTimer? _saveTimer;
    private bool _pendingConfigure;
    private bool _pendingRestart;

    private void QueueSave(string key, object? value, bool configure = false, bool restart = false)
    {
        _pendingPatch[key] = value!;
        _pendingConfigure |= configure;
        _pendingRestart |= restart;
        if (_saveTimer is null)
        {
            _saveTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(350) };
            _saveTimer.Tick += async (_, _) =>
            {
                _saveTimer.Stop();
                var patch = new Dictionary<string, object>(_pendingPatch);
                _pendingPatch.Clear();
                var cfg = _pendingConfigure; _pendingConfigure = false;
                var rst = _pendingRestart; _pendingRestart = false;
                await SaveSettingsAsync(patch, configure: cfg);
                if (rst && _player is not null) await _player.RestartCurrentAsync();
            };
        }
        _saveTimer.Stop();
        _saveTimer.Start();
    }

    private bool? _settingsWide;

    private void SettingsView_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        UpdateSettingsLayout(e.NewSize.Width);
    }

    private void UpdateSettingsLayout(double viewportWidth)
    {
        if (viewportWidth <= 0) return;
        // ScrollViewer 量内容时给的是自然宽而不是视口宽；这里按视口给一个确定宽度并居中。
        // 宽窗把设置拆成两列，否则 1100+ 的内容区会剩一整条无法解释的空白。
        var width = Math.Clamp(viewportWidth - 48, 380, 1160);
        SettingsColumns.Width = width;
        var wide = width >= 900;
        if (_settingsWide == wide) return;
        _settingsWide = wide;

        SettingsGapColumn.Width = new GridLength(wide ? 16 : 0);
        SettingsRightColumn.Width = wide ? new GridLength(1, GridUnitType.Star) : new GridLength(0);
        SettingsColRight.Visibility = wide ? Visibility.Visible : Visibility.Collapsed;
        var moved = new[] { SecLyrics, SecLibrary, SecExcludes, SecAbout };
        foreach (var section in moved)
        {
            // Add 一个已有父级的元素会抛异常；先查归属，保证重复调用安全。
            if (wide)
            {
                if (SettingsColLeft.Children.Contains(section)) SettingsColLeft.Children.Remove(section);
                if (!SettingsColRight.Children.Contains(section)) SettingsColRight.Children.Add(section);
            }
            else
            {
                if (SettingsColRight.Children.Contains(section)) SettingsColRight.Children.Remove(section);
                // 单列保持从左列阅读到底的顺序：外观→播放→音质→均衡器→歌词→音乐库→排除→关于。
                if (!SettingsColLeft.Children.Contains(section)) SettingsColLeft.Children.Add(section);
            }
        }
        Trace($"settings layout wide={wide} width={width:0}");
    }

    private async Task ShowSettingsAsync()
    {
        _viewMode = "settings";
        ConfigureHeader("设置", "偏好、音质与音乐库", sort: false, actions: false);
        // 设置页没有可搜索的内容；顺手把搜索与 A–Z 收起，页头只留标题。
        SearchHost.Visibility = Visibility.Collapsed;
        ShowOnly(SettingsView);
        UpdateSettingsLayout(ContentHost.ActualWidth);
        SettingsView.ChangeView(null, 0, null, true);
        // 先用 boot 数据立即铺满控件，网络刷新后再更新一次，避免半加载的空页面。
        BuildEqBands();
        PopulateSettings();
        RenderLibraries();
        FillAbout();
        try
        {
            var resp = await _api!.GetAsync<SettingsResponse>("settings");
            _set = resp.Settings ?? new SettingsDto();
        }
        catch { _set = _boot?.Settings ?? new SettingsDto(); }
        try
        {
            var dev = await _api!.GetAsync<DevicesResponse>("native/devices");
            _devices.Clear();
            if (dev.Ok) _devices.AddRange(dev.Items);
        }
        catch { _devices.Clear(); }
        PopulateSettings();
        await RefreshExcludesAsync();
        await PollScanAsync();
    }

    private void PopulateSettings()
    {
        _settingsPopulating = true;
        try
        {
            var theme = _set.Theme is "light" or "system" ? _set.Theme : "dark";
            SelectByTag(SetTheme, theme);
            if (SetTheme.SelectedIndex < 0) SelectByTag(SetTheme, "dark");
            SetCloseToTray.IsOn = _set.CloseToTray;
            SetResume.IsOn = _set.ResumeOnStart;
            SelectByTag(SetPlayMode, string.IsNullOrWhiteSpace(_set.PlayMode) ? "list_loop" : _set.PlayMode);
            var rate = Math.Clamp(_set.Rate <= 0 ? 1 : _set.Rate, 0.5, 2);
            SetRate.Value = rate;
            SetRateText.Text = rate.ToString("0.00") + "×";

            SetDevice.Items.Clear();
            SetDevice.Items.Add(new ComboBoxItem { Content = "跟随系统默认", Tag = "" });
            foreach (var d in _devices)
            {
                SetDevice.Items.Add(new ComboBoxItem
                {
                    Content = d.Name + (d.IsDefault ? "（默认）" : ""),
                    Tag = d.Index.ToString(),
                });
            }
            var devTag = _set.AudioDevice?.ToString() ?? "";
            SelectByTag(SetDevice, devTag);
            if (SetDevice.SelectedIndex < 0) SetDevice.SelectedIndex = 0;

            SetExclusive.IsOn = _set.AudioExclusive;
            SetTargetRate.IsEnabled = _set.AudioExclusive;
            SetTargetRateRow.Opacity = _set.AudioExclusive ? 1 : 0.4;
            SelectByTag(SetTargetRate, (_set.AudioTargetRate).ToString());
            if (SetTargetRate.SelectedIndex < 0) SetTargetRate.SelectedIndex = 0;
            SetDither.IsOn = _set.AudioDither;
            SetLoudness.IsOn = _set.AudioLoudness;
            SetBalance.Value = Math.Clamp(_set.AudioBalance, -1, 1);
            SetBalanceText.Text = BalanceText(_set.AudioBalance);
            SetFade.Value = Math.Clamp(_set.AudioFadeIn, 0, 3);
            SetFadeText.Text = _set.AudioFadeIn.ToString("0.0") + " 秒";
            SetSpeakers.IsOn = _set.AudioSpeakers;

            SetEqEnabled.IsOn = _set.EqEnabled;
            SetEqBandsEnabled(_set.EqEnabled);
            EqBands.Opacity = _set.EqEnabled ? 1 : 0.4;
            var gains = (_set.EqGains is { Length: >= 10 } ? _set.EqGains : EqFlat).Take(10).ToArray();
            for (var i = 0; i < _eqSliders.Count; i++)
            {
                _eqSliders[i].Value = Math.Clamp(gains[i], -12, 12);
                if (_eqSliders[i].Tag is TextBlock tb) tb.Text = GainText(gains[i]);
            }
            SelectByTag(SetEqPreset, string.IsNullOrWhiteSpace(_set.EqPreset) ? "flat" : _set.EqPreset);
            if (SetEqPreset.SelectedIndex < 0) SelectByTag(SetEqPreset, "custom");

            var sizePct = _set.LyricSize is > 40 and < 220 ? _set.LyricSize : 100;
            SetLyricSize.Value = Math.Clamp(sizePct, 75, 160);
            SetLyricSizeText.Text = ((int)Math.Round(sizePct)) + "%";
            SetTranslation.IsOn = _set.ShowTranslation;
            SetPlaybarLyricToggle.IsOn = _set.PlaybarLyric;
            SetDesktopLyric.IsOn = _set.DesktopLyric;
            SetDesktopLyricTop.IsOn = _set.DesktopLyricOnTop;
            SetDesktopLyricTop.IsEnabled = _set.DesktopLyric;
            SetUnsupported.IsOn = _set.IncludeUnsupported;
        }
        finally { _settingsPopulating = false; }
    }

    private static void SelectByTag(ComboBox box, string tag)
    {
        for (var i = 0; i < box.Items.Count; i++)
        {
            if (box.Items[i] is ComboBoxItem item && (item.Tag as string) == tag)
            {
                box.SelectedIndex = i;
                return;
            }
        }
    }

    private static string ComboTag(ComboBox box) =>
        (box.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    private static string BalanceText(double v) =>
        Math.Abs(v) < 0.03 ? "居中" : v < 0 ? $"偏左 {Math.Abs(v) * 100:0}%" : $"偏右 {v * 100:0}%";

    private static string GainText(double g) => g.ToString("+0.#;-0.#;0");

    private void BuildEqBands()
    {
        if (_eqSliders.Count > 0) return;
        EqBands.ColumnDefinitions.Clear();
        for (var i = 0; i < 10; i++)
        {
            EqBands.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var panel = new StackPanel { HorizontalAlignment = HorizontalAlignment.Center, Spacing = 5 };
            var val = new TextBlock
            {
                Text = "0",
                FontSize = 10.5,
                HorizontalAlignment = HorizontalAlignment.Center,
                Foreground = _dimBrush,
            };
            var slider = new Slider
            {
                Orientation = Orientation.Vertical,
                Minimum = -12,
                Maximum = 12,
                StepFrequency = 0.5,
                Height = 110,
                HorizontalAlignment = HorizontalAlignment.Center,
                IsThumbToolTipEnabled = false,
                Tag = val,
            };
            slider.ValueChanged += EqBand_Changed;
            var freq = new TextBlock
            {
                Text = EqFreqs[i],
                FontSize = 10,
                HorizontalAlignment = HorizontalAlignment.Center,
                Foreground = _dimBrush,
            };
            panel.Children.Add(val);
            panel.Children.Add(slider);
            panel.Children.Add(freq);
            Grid.SetColumn(panel, i);
            EqBands.Children.Add(panel);
            _eqSliders.Add(slider);
        }
    }

    private void SetEqBandsEnabled(bool on)
    {
        // Grid 是 Panel，没有 IsEnabled；置灰要落到每个滑杆上，容器只做透明度。
        foreach (var slider in _eqSliders) slider.IsEnabled = on;
    }

    private void ApplyEqGains(double[] gains)
    {
        _settingsPopulating = true;
        for (var i = 0; i < _eqSliders.Count && i < gains.Length; i++)
        {
            _eqSliders[i].Value = Math.Clamp(gains[i], -12, 12);
            if (_eqSliders[i].Tag is TextBlock tb) tb.Text = GainText(gains[i]);
        }
        _settingsPopulating = false;
        _set.EqGains = gains.Take(10).ToArray();
    }

    private void RenderLibraries()
    {
        LibraryList.Children.Clear();
        var libs = _boot?.Libraries ?? new List<LibraryInfo>();
        if (libs.Count == 0)
        {
            LibraryList.Children.Add(new TextBlock
            {
                Text = "还没有音乐文件夹，点右上角「添加文件夹」开始。",
                Style = (Style)Application.Current.Resources["CaptionText"],
            });
            return;
        }
        foreach (var lib in libs)
        {
            var row = new Grid { ColumnSpacing = 10 };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var info = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Spacing = 1 };
            info.Children.Add(new TextBlock { Text = lib.DisplayName, Style = (Style)Application.Current.Resources["TitleText"] });
            info.Children.Add(new TextBlock
            {
                Text = $"{lib.Path} · {lib.TrackCount} 首",
                Style = (Style)Application.Current.Resources["CaptionText"],
                TextTrimming = TextTrimming.CharacterEllipsis,
            });
            Grid.SetColumn(info, 0);
            row.Children.Add(info);

            var rename = new Button
            {
                Style = (Style)Application.Current.Resources["IconButtonStyle"],
                Width = 32, Height = 32,
                Content = new FontIcon { Glyph = "\uE70F", FontSize = 12 },
                Tag = lib.Id,
            };
            ToolTipService.SetToolTip(rename, "重命名文件夹");
            rename.Click += async (_, _) =>
            {
                var name = await PromptTextAsync("重命名文件夹", $"当前：{lib.DisplayName}", lib.DisplayName, "保存");
                if (string.IsNullOrWhiteSpace(name)) return;
                try
                {
                    await _api!.PostAsync("library/rename", new { id = lib.Id, name });
                    _boot = await _api.GetAsync<BootstrapResponse>("bootstrap");
                    RenderLibraries();
                    ShowToast("已重命名为：" + name.Trim());
                }
                catch (Exception e) { ShowToast("重命名失败：" + e.Message); }
            };
            Grid.SetColumn(rename, 1);
            row.Children.Add(rename);

            var rescan = new Button
            {
                Style = (Style)Application.Current.Resources["IconButtonStyle"],
                Width = 32, Height = 32,
                Content = new FontIcon { Glyph = "\uE72C", FontSize = 13 },
                Tag = lib.Id,
            };
            ToolTipService.SetToolTip(rescan, "重新扫描");
            rescan.Click += async (_, _) =>
            {
                try
                {
                    await _api!.PostAsync("scan", new { id = lib.Id });
                    StartScanPoll();
                    ShowToast("正在扫描：" + lib.DisplayName);
                }
                catch (Exception e) { ShowToast("扫描失败：" + e.Message); }
            };
            Grid.SetColumn(rescan, 2);
            row.Children.Add(rescan);

            var remove = new Button
            {
                Style = (Style)Application.Current.Resources["IconButtonStyle"],
                Width = 32, Height = 32,
                Content = new FontIcon { Glyph = "\uE74D", FontSize = 13 },
                Tag = lib.Id,
            };
            ToolTipService.SetToolTip(remove, "移除文件夹");
            remove.Click += async (_, _) => await RemoveLibraryAsync(lib);
            Grid.SetColumn(remove, 3);
            row.Children.Add(remove);
            LibraryList.Children.Add(row);
        }
    }

    private async Task RemoveLibraryAsync(LibraryInfo lib)
    {
        var dialog = new ContentDialog
        {
            Title = "移除文件夹",
            Content = $"从音乐库移除「{lib.DisplayName}」？\n磁盘上的文件不会被删除。",
            PrimaryButtonText = "移除",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = Content.XamlRoot,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        try
        {
            await _api!.PostAsync("library/remove", new { id = lib.Id, keep_tracks = false });
            await ReloadLibraryAsync();
            ShowToast("已移除：" + lib.DisplayName);
        }
        catch (Exception e) { ShowToast("移除失败：" + e.Message); }
    }

    // ---- 扫描状态 / 排除目录 ----
    private void StartScanPoll()
    {
        ScanStatusText.Text = "扫描中…";
        if (!_scanPoll.IsEnabled) _scanPoll.Start();
    }

    private async Task PollScanAsync()
    {
        ScanStatusResponse? st = null;
        try { st = await _api!.GetAsync<ScanStatusResponse>("scan"); } catch { }
        if (st is null)
        {
            _scanPoll.Stop();
            ScanStatusText.Text = "无法获取扫描状态";
            return;
        }
        if (st.Running)
        {
            var phase = st.Phase switch
            {
                "walking" => "查找文件",
                "reading" => "读取曲目",
                "writing" => "写入音乐库",
                "done" => "收尾",
                _ => st.Phase ?? "扫描",
            };
            ScanStatusText.Text = st.Total > 0
                ? $"扫描中（{phase}）· {Math.Min(st.Done, st.Total)}/{st.Total}"
                : $"扫描中（{phase}）";
            _scanBusy = true;
            if (!_scanPoll.IsEnabled) _scanPoll.Start();
            return;
        }
        _scanPoll.Stop();
        ScanStatusText.Text = st.FinishedAt > 0
            ? $"上次扫描：新增 {st.Added} · 更新 {st.Updated} · 移除 {st.Removed}"
            : "扫描空闲";
        if (!_scanBusy) return;
        _scanBusy = false;
        await ReloadLibraryAsync();
        ShowToast($"扫描完成：新增 {st.Added} · 更新 {st.Updated} · 移除 {st.Removed}");
    }

    private async Task<bool> StartScanAsync(object payload)
    {
        try
        {
            var resp = await _api!.PostAsync<ScanStartResponse>("scan", payload);
            if (!resp.Ok)
            {
                if (resp.Busy) { ShowToast("已有扫描在进行"); StartScanPoll(); return false; }
                ShowToast("无法开始扫描" + (string.IsNullOrWhiteSpace(resp.Error) ? "" : "：" + resp.Error));
                return false;
            }
            StartScanPoll();
            return true;
        }
        catch (Exception e)
        {
            ShowToast("扫描失败：" + e.Message);
            return false;
        }
    }

    private async Task ReloadLibraryAsync()
    {
        _allTracks.Clear();
        _recent.Clear();
        _fresh.Clear();
        _albumsAll.Clear();
        _artistsAll.Clear();
        _azTracks.Clear();
        _azLetters.Clear();
        AzRail.ItemsSource = null;
        _azMode = false;
        try
        {
            _boot = await _api!.GetAsync<BootstrapResponse>("bootstrap");
            AppServices.Bootstrap = _boot;
            ApplyBootstrap(_boot);
            await LoadLibraryAsync();
            await RefreshPlaylistsAsync();
            RenderLibraries();
            FillAbout();
            switch (_viewMode)
            {
                case "songs": ShowSongs(); break;
                case "albums": ShowAlbums(); break;
                case "artists": ShowArtists(); break;
            }
        }
        catch (Exception e) { ShowToast("刷新音乐库失败：" + e.Message); }
    }

    private async void RescanAll_Click(object sender, RoutedEventArgs e)
    {
        if (await StartScanAsync(new { })) ShowToast("正在重新扫描全部音乐文件夹");
    }

    private async void PurgeMissing_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new ContentDialog
        {
            Title = "清理失效条目",
            Content = "把磁盘上已不存在的文件从音乐库移除？文件本身不会被删除。",
            PrimaryButtonText = "清理",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = Content.XamlRoot,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        try
        {
            var resp = await _api!.PostAsync<PurgeResponse>("purge", new { });
            await ReloadLibraryAsync();
            ShowToast(resp.Removed > 0 ? $"已清理 {resp.Removed} 条失效条目" : "没有发现失效条目");
        }
        catch (Exception ex) { ShowToast("清理失败：" + ex.Message); }
    }

    private async Task RefreshExcludesAsync()
    {
        try
        {
            var resp = await _api!.GetAsync<ExcludesResponse>("excludes");
            _excludes = resp.Items ?? new List<string>();
        }
        catch { _excludes = new List<string>(); }
        RenderExcludes();
    }

    private void RenderExcludes()
    {
        ExcludeList.Children.Clear();
        if (_excludes.Count == 0)
        {
            ExcludeList.Children.Add(new TextBlock
            {
                Text = "没有排除任何文件夹，扫描会遍历音乐文件夹下的全部目录。",
                Style = (Style)Application.Current.Resources["CaptionText"],
            });
            return;
        }
        foreach (var path in _excludes)
        {
            var row = new Grid { ColumnSpacing = 10 };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new TextBlock
            {
                Text = path,
                VerticalAlignment = VerticalAlignment.Center,
                Style = (Style)Application.Current.Resources["SubText"],
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            Grid.SetColumn(text, 0);
            row.Children.Add(text);

            var del = new Button
            {
                Style = (Style)Application.Current.Resources["IconButtonStyle"],
                Width = 32, Height = 32,
                Content = new FontIcon { Glyph = "\uE711", FontSize = 11 },
            };
            ToolTipService.SetToolTip(del, "取消排除");
            var target = path;
            del.Click += async (_, _) =>
            {
                try
                {
                    var resp = await _api!.PostAsync<ExcludesResponse>("excludes/remove", new { path = target });
                    _excludes = resp.Items ?? new List<string>();
                    RenderExcludes();
                    ShowToast("已取消排除：" + target);
                }
                catch (Exception e) { ShowToast("操作失败：" + e.Message); }
            };
            Grid.SetColumn(del, 1);
            row.Children.Add(del);
            ExcludeList.Children.Add(row);
        }
    }

    private async void AddExclude_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return;
            var resp = await _api!.PostAsync<ExcludesResponse>("excludes/add", new { path = folder.Path });
            if (!resp.Ok)
            {
                ShowToast(resp.Error ?? "添加排除失败");
                return;
            }
            _excludes = resp.Items ?? new List<string>();
            RenderExcludes();
            if (resp.Already)
            {
                ShowToast("该文件夹已在排除范围内");
                return;
            }
            ShowToast("已排除，正在重新扫描以生效：" + folder.Path);
            await StartScanAsync(new { });
        }
        catch (Exception ex) { ShowToast("添加排除失败：" + ex.Message); }
    }

    private void FillAbout()
    {
        var app = _boot?.App;
        AboutVersion.Text = app is null ? "WinUI 3 原生壳" : $"版本 {app.Version} · WinUI 3 原生";
        var st = _boot?.Stats;
        AboutStats.Text = st is null ? "" :
            $"{st.Tracks:N0} 首 · {st.Albums:N0} 专辑 · {st.Artists:N0} 艺术家 · {FormatLong(st.Duration)}";
        AboutEngine.Text = "音频引擎：ffmpeg 浮点解码 → WASAPI（原生）";
    }

    // ---- 外观 ----
    private void SetTheme_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        var tag = ComboTag(SetTheme);
        if (tag.Length == 0) return;
        _set.Theme = tag;
        ApplyTheme(tag);
        QueueSave("theme", tag);
    }

    private void SetCloseToTray_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        _set.CloseToTray = SetCloseToTray.IsOn;
        QueueSave("close_to_tray", SetCloseToTray.IsOn);
    }

    // ---- 播放 ----
    private void SetResume_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        QueueSave("resume_on_start", SetResume.IsOn);
    }

    private void SetPlayMode_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingsPopulating || _player is null) return;
        var tag = ComboTag(SetPlayMode);
        if (tag.Length == 0) return;
        _set.PlayMode = tag;
        _ = _player.SetPlayModeAsync(tag);
    }

    private void SetRate_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        var v = Math.Clamp(e.NewValue, 0.5, 2);
        SetRateText.Text = v.ToString("0.00") + "×";
        _set.Rate = v;
        QueueSave("rate", v, restart: true);
    }

    // ---- 音质 / 输出 ----
    private void SetDevice_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        var tag = ComboTag(SetDevice);
        int? dev = tag.Length == 0 ? null : int.Parse(tag);
        _set.AudioDevice = dev;
        QueueSave("audio_device", dev, restart: true);
    }

    private void SetExclusive_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        SetTargetRate.IsEnabled = SetExclusive.IsOn;
        SetTargetRateRow.Opacity = SetExclusive.IsOn ? 1 : 0.4;
        _set.AudioExclusive = SetExclusive.IsOn;
        QueueSave("audio_exclusive", SetExclusive.IsOn, restart: true);
    }

    private void SetTargetRate_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        if (int.TryParse(ComboTag(SetTargetRate), out var hz))
        {
            _set.AudioTargetRate = hz;
            QueueSave("audio_target_rate", hz, restart: true);
        }
    }

    private void SetDither_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        QueueSave("audio_dither", SetDither.IsOn, configure: true);
    }

    private void SetLoudness_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        QueueSave("audio_loudness", SetLoudness.IsOn, configure: true);
    }

    private void SetBalance_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        SetBalanceText.Text = BalanceText(e.NewValue);
        _set.AudioBalance = e.NewValue;
        QueueSave("audio_balance", e.NewValue, configure: true);
    }

    private void SetFade_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        SetFadeText.Text = e.NewValue.ToString("0.0") + " 秒";
        _set.AudioFadeIn = e.NewValue;
        QueueSave("audio_fade_in", e.NewValue, configure: true);
    }

    private void SetSpeakers_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        QueueSave("audio_speakers", SetSpeakers.IsOn, configure: true);
    }

    // ---- 均衡器 ----
    private void SetEqEnabled_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        SetEqBandsEnabled(SetEqEnabled.IsOn);
        EqBands.Opacity = SetEqEnabled.IsOn ? 1 : 0.4;
        _set.EqEnabled = SetEqEnabled.IsOn;
        QueueSave("eq_enabled", SetEqEnabled.IsOn, configure: true);
    }

    private void SetEqPreset_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        var tag = ComboTag(SetEqPreset);
        if (tag.Length == 0 || tag == "custom") return;   // 自定义＝保留手拖的曲线
        var gains = EqPresets.TryGetValue(tag, out var g) ? g : EqFlat;
        ApplyEqGains(gains);
        _set.EqPreset = tag;
        QueueSave("eq_gains", _set.EqGains, configure: true);
        QueueSave("eq_preset", tag, configure: true);
    }

    private void EqBand_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_settingsPopulating || sender is not Slider sl) return;
        var idx = _eqSliders.IndexOf(sl);
        if (idx < 0) return;
        if (sl.Tag is TextBlock tb) tb.Text = GainText(e.NewValue);
        var gains = (_set.EqGains is { Length: >= 10 } ? _set.EqGains : EqFlat).Take(10).ToArray();
        gains[idx] = e.NewValue;
        _set.EqGains = gains;
        _set.EqPreset = "custom";
        // 手拖任意一段即脱离预设；这里改 ComboBox 会触发 Preset_Changed，但 custom 直接 return。
        _settingsPopulating = true;
        SelectByTag(SetEqPreset, "custom");
        _settingsPopulating = false;
        QueueSave("eq_gains", gains, configure: true);
        QueueSave("eq_preset", "custom", configure: true);
    }

    // ---- 歌词 ----
    private void SetLyricSize_Changed(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_settingsPopulating) return;
        SetLyricSizeText.Text = ((int)Math.Round(e.NewValue)) + "%";
        _set.LyricSize = e.NewValue;
        ApplyLyricSettings(_set);
        QueueSave("lyric_size", e.NewValue);
    }

    private void SetTranslation_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        _set.ShowTranslation = SetTranslation.IsOn;
        ApplyLyricSettings(_set);
        QueueSave("show_translation", SetTranslation.IsOn);
    }

    private void SetPlaybarLyric_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        _set.PlaybarLyric = SetPlaybarLyricToggle.IsOn;
        ApplyLyricSettings(_set);
        QueueSave("playbar_lyric", SetPlaybarLyricToggle.IsOn);
    }

    private void SetDesktopLyric_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        // Show/Hide 内部会回写 IsOn，这里按 _set 判断避免自触发二次进入。
        if (SetDesktopLyric.IsOn == _set.DesktopLyric) return;
        if (SetDesktopLyric.IsOn) ShowMiniLyrics(persist: false);
        else HideMiniLyrics(persist: false);
        SetDesktopLyricTop.IsEnabled = SetDesktopLyric.IsOn;
        QueueSave("desktop_lyric", SetDesktopLyric.IsOn);
    }

    private void SetDesktopLyricTop_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        _set.DesktopLyricOnTop = SetDesktopLyricTop.IsOn;
        _mini?.SetAlwaysOnTop(SetDesktopLyricTop.IsOn);
        QueueSave("desktop_lyric_on_top", SetDesktopLyricTop.IsOn);
    }

    // ---- 音乐库 ----
    private void SetUnsupported_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingsPopulating) return;
        QueueSave("include_unsupported", SetUnsupported.IsOn);
    }

    private async void AddLibrary_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return;
            await _api!.PostAsync("library/add", new { path = folder.Path, scan = true });
            _boot = await _api.GetAsync<BootstrapResponse>("bootstrap");
            RenderLibraries();
            StartScanPoll();
            ShowToast("已添加，正在扫描：" + folder.Name);
        }
        catch (Exception ex) { ShowToast("添加失败：" + ex.Message); }
    }

    private void SyncPlayerUi()
    {
        if (_player is null) return;
        var current = _player.Current;
        var id = current?.Id ?? 0;
        if (id != _nowTrackId)
        {
            _nowTrackId = id;
            UpdateNowPlaying(current);
        }
        UpdateTransport();
        UpdateVolume();
        var qc = _player.Queue.Count;
        if (qc != _lastQueueCount)
        {
            _lastQueueCount = qc;
            QueueCount.Text = $"{qc} 首";
            QueueEmpty.Visibility = qc == 0 ? Visibility.Visible : Visibility.Collapsed;
            QueueClearButton.IsEnabled = qc > 0;
        }
        // 面板打开时换行自动跟随居中；关着时 scroll 传 false，只刷播放条歌词。
        SyncLyric(scroll: _lyricOpen);
    }

    private void UpdateNowPlaying(Track? track)
    {
        NowTitle.Text = track?.TitleText ?? "未在播放";
        _nowSub = track is null ? "选一首开始" : $"{track.ArtistText} · {track.AlbumText}";
        NowArtist.Text = _nowSub;
        TitleNowPlaying.Text = track is null ? "" : $"{track.TitleText} — {track.ArtistText}";
        SetArt(NowArt, track, "art", 160);
        _qualityFull = track is null ? "" : QualityOf(track);
        _qualityShort = track is null ? "" : QualityShortOf(track);
        QualityBadge.Text = _playerCompact == true ? _qualityShort : _qualityFull;
        SyncNowFavorite();
        _mini?.SetTrack(track?.TitleText, track?.ArtistText, _player?.IsPlaying == true);
        _ = LoadLyricsAsync(track);
    }

    private void UpdateTransport()
    {
        var player = _player;
        var playing = player?.IsPlaying == true;
        _mini?.SetPlaying(playing);
        PlayGlyph.Glyph = playing ? "\uE769" : "\uE768";
        HeroPlayGlyph.Glyph = playing ? "\uE769" : "\uE768";
        HeroPlayText.Text = playing ? "暂停" : "播放";

        var pos = player?.Position ?? 0;
        var dur = player?.Duration ?? 0;

        _syncingSeek = true;
        SeekSlider.Maximum = dur > 0 ? dur : 1;
        if (!_seekDragging)
        {
            SeekSlider.Value = Math.Clamp(pos, 0, SeekSlider.Maximum);
        }
        _syncingSeek = false;

        TimeNow.Text = FormatTime(_seekDragging ? SeekSlider.Value : pos);
        TimeTotal.Text = FormatTime(dur);

        if (player is null) return;
        var mode = player.PlayMode;
        ShuffleToggle.Foreground = mode == "shuffle" ? _accentBrush : _dimBrush;
        RepeatGlyph.Glyph = mode == "one_loop" ? "\uE8ED" : "\uE8EE";
        RepeatToggle.Foreground = mode == "normal" ? _dimBrush : _accentBrush;
        ToolTipService.SetToolTip(ShuffleToggle, mode == "shuffle" ? "随机播放：开启" : "随机播放：关闭");
        ToolTipService.SetToolTip(RepeatToggle, mode switch
        {
            "one_loop" => "单曲循环",
            "normal" => "顺序播放",
            _ => "列表循环",
        });
        HeroKind.Text = player.Current is null ? "为你准备" : playing ? "正在播放" : "已暂停";
    }

    private void UpdateVolume()
    {
        if (_player is null) return;
        _syncingVolume = true;
        VolumeSlider.Value = Math.Clamp(_player.Volume, 0, 1);
        _syncingVolume = false;
        var muted = _player.Muted || _player.Volume <= 0.001;
        VolumeGlyph.Glyph = muted ? "\uE74F" : "\uE767";
        ToolTipService.SetToolTip(VolumeButton, muted ? "取消静音" : "静音");
    }

    private void SyncNowFavorite()
    {
        var favorite = _player?.Current?.Favorite == true;
        NowFavoriteGlyph.Glyph = favorite ? "\uE00B" : "\uE006";
        NowFavoriteGlyph.Foreground = favorite ? _accentBrush : _dimBrush;
    }

    private void SyncHero()
    {
        var track = _player?.Current ?? _recent.FirstOrDefault() ?? _allTracks.FirstOrDefault();
        if (track is null)
        {
            HeroTitle.Text = "曲库是空的";
            HeroSub.Text = "先添加一个音乐文件夹";
            HeroKind.Text = "此刻";
            SetArt(HeroArt, null, "art", 360);
            return;
        }
        HeroKind.Text = _player?.Current is null ? "为你准备" : (_player.IsPlaying ? "正在播放" : "已暂停");
        HeroTitle.Text = track.TitleText;
        HeroSub.Text = $"{track.ArtistText} · {track.AlbumText}";
        SetArt(HeroArt, track, "full", 360);
    }

    private void SetArt(Image image, Track? track, string size, int decode)
    {
        // 占位块一直是垫在图下面的 underlay：网络/解码失败时露出来的是图标，
        // 而不是一块空白；成功加载的封面完全不透明，盖住它不影响观感。
        image.Tag = null;
        image.Source = track is null || _api is null
            ? null
            : new BitmapImage
            {
                DecodePixelWidth = decode,
                UriSource = new Uri(_api.TrackCover(track.Id, size)),
            };
    }

    private async void CoverImage_Failed(object sender, ExceptionRoutedEventArgs e)
    {
        if (sender is Image image) await RetryImageAsync(image);
    }

    private async void ArtImage_Failed(object sender, ExceptionRoutedEventArgs e)
    {
        if (sender is Image image) await RetryImageAsync(image);
    }

    private async Task RetryImageAsync(Image image)
    {
        if (image.Source is not BitmapImage failed || failed.UriSource is not { } uri) return;
        var attempt = image.Tag is int n ? n : 0;
        if (attempt >= 2) return;
        image.Tag = attempt + 1;
        await Task.Delay(320 + attempt * 480);
        DispatcherQueue.TryEnqueue(() =>
        {
            // 列表在重试窗口里可能已经把这个容器绑到别的曲目上了
            if (!image.IsLoaded || !ReferenceEquals(image.Source, failed)) return;
            var sep = uri.Query.Length > 1 ? "&" : "?";
            var retry = new BitmapImage
            {
                DecodePixelWidth = failed.DecodePixelWidth,
                UriSource = new Uri(uri.OriginalString + sep + "retry=" + (attempt + 1)),
            };
            image.Source = retry;
        });
    }

    private static string QualityOf(Track t)
    {
        var parts = new List<string>(3);
        if (!string.IsNullOrWhiteSpace(t.Fmt)) parts.Add(t.Fmt!.ToUpperInvariant());
        if (t.SampleRate is > 0) parts.Add($"{t.SampleRate.Value / 1000.0:0.#} kHz");
        if (t.Bitrate is > 0) parts.Add($"{t.Bitrate.Value / 1000} kbps");
        return string.Join(" · ", parts);
    }

    private static string QualityShortOf(Track t)
    {
        if (!string.IsNullOrWhiteSpace(t.Fmt)) return t.Fmt!.ToUpperInvariant();
        if (t.SampleRate is > 0) return $"{t.SampleRate.Value / 1000.0:0.#} kHz";
        return "";
    }

    private static string FormatTime(double seconds)
    {
        if (double.IsNaN(seconds) || double.IsInfinity(seconds) || seconds < 0) seconds = 0;
        var ts = TimeSpan.FromSeconds(seconds);
        return ts.TotalHours >= 1
            ? $"{(int)ts.TotalHours}:{ts.Minutes:00}:{ts.Seconds:00}"
            : $"{ts.Minutes}:{ts.Seconds:00}";
    }

    private static string FormatLong(double seconds)
    {
        var ts = TimeSpan.FromSeconds(Math.Max(0, seconds));
        if (ts.TotalHours >= 1) return $"{(int)ts.TotalHours} 小时 {ts.Minutes} 分";
        return $"{Math.Max(1, ts.Minutes)} 分钟";
    }

    private Action? _toastAction;

    private void ShowToast(string message) => ShowToast(message, null, null);

    private void ShowToast(string message, string? actionLabel, Action? action)
    {
        if (!DispatcherQueue.HasThreadAccess)
        {
            DispatcherQueue.TryEnqueue(() => ShowToast(message, actionLabel, action));
            return;
        }
        if (string.IsNullOrWhiteSpace(message)) return;
        ToastText.Text = message.Length > 240 ? message[..240] + "…" : message;
        _toastAction = action;
        ToastAction.Content = actionLabel ?? "";
        ToastAction.Visibility = action is null ? Visibility.Collapsed : Visibility.Visible;
        ToastPanel.IsHitTestVisible = action is not null;
        ToastPanel.Visibility = Visibility.Visible;
        _toastTimer.Stop();
        _toastTimer.Interval = TimeSpan.FromSeconds(action is null ? 2.4 : 6.5);
        _toastTimer.Start();
    }

    private void HideToast()
    {
        _toastTimer.Stop();
        _toastAction = null;
        ToastAction.Visibility = Visibility.Collapsed;
        ToastPanel.IsHitTestVisible = false;
        ToastPanel.Visibility = Visibility.Collapsed;
    }

    private void ToastAction_Click(object sender, RoutedEventArgs e)
    {
        var action = _toastAction;
        HideToast();
        action?.Invoke();
    }

    private void OpenLog_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var path = _backend?.LogPath;
            if (path is not null && File.Exists(path))
            {
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
        }
        catch (Exception ex)
        {
            ShowToast("无法打开日志：" + ex.Message);
        }
    }

    // ------------------------------------------------------------- shutdown

    private void OnAppWindowClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_closeAllowed) return;
        args.Cancel = true;
        if (_closing) return;
        if (_set.CloseToTray && _tray?.IsAdded == true)
        {
            HideToTray();
            return;
        }
        _closing = true;
        _ = ShutdownAsync();
    }

    private async Task ShutdownAsync()
    {
        try
        {
            await SaveWindowPlacementAsync();
        }
        catch
        {
        }
        try
        {
            if (_player is not null) await _player.FlushAsync();
        }
        catch
        {
        }
        _player?.Shutdown();
        _api?.Dispose();
        if (_backend is not null)
        {
            await Task.Run(_backend.Dispose);
        }
        _mini?.Close();
        _mini = null;
        _media?.Dispose();
        _media = null;
        _tray?.Dispose();
        _tray = null;
        _closeAllowed = true;
        Close();
    }

    private void OnClosed(object sender, WindowEventArgs args)
    {
        _mini?.Close();
        _mini = null;
        _media?.Dispose();
        _media = null;
        _player?.Shutdown();
        _api?.Dispose();
        _backend?.Dispose();
        _tray?.Dispose();
        _tray = null;
    }
}
