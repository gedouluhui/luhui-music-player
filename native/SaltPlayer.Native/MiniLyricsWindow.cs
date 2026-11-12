using System.Runtime.InteropServices;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.Foundation;
using Windows.Graphics;
using Windows.UI;

namespace SaltPlayer.Native;

/// <summary>桌面歌词：无边框、置顶、可拖动的悬浮条，跟随播放实时换行。</summary>
internal sealed class MiniLyricsWindow : Window
{
    private const double BaseFont = 23;
    private const double MinScale = 0.6;
    private const double MaxScale = 2.0;

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    private readonly Grid _root = new();
    private readonly Border _frame;
    private readonly TextBlock _line = new();
    private readonly StackPanel _bar = new();
    private readonly SolidColorBrush _tint;
    private readonly Button _pinButton;

    private double _scale;
    private bool _onTop;
    private bool _playing = true;
    private bool _backdropOk;
    private string _lyric = "";
    private string _title = "";
    private string _artist = "";

    private bool _dragging;
    private Point _dragOrigin;
    private PointInt32 _dragWindowOrigin;

    public event Action? CloseRequested;
    public event Action<bool>? TopChanged;
    public event Action<double>? ScaleChanged;
    public event Action<RectInt32>? Moved;

    public FrameworkElement Root => _root;

    public MiniLyricsWindow(double scale, bool onTop, bool light,
                            int? x, int? y, int width, int height)
    {
        Title = "桌面歌词";
        _scale = Math.Clamp(scale <= 0 ? 1 : scale, MinScale, MaxScale);
        _onTop = onTop;

        _root.RequestedTheme = light ? ElementTheme.Light : ElementTheme.Dark;
        _root.Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent);
        Content = _root;

        _tint = new SolidColorBrush(light
            ? Color.FromArgb(0x8C, 0xFF, 0xFF, 0xFF)
            : Color.FromArgb(0x74, 0x0B, 0x0F, 0x16));
        _frame = new Border
        {
            CornerRadius = new CornerRadius(8),
            Background = _tint,
            BorderBrush = (Brush)Application.Current.Resources["HairlineStrongBrush"],
            BorderThickness = new Thickness(1),
        };
        _root.Children.Add(_frame);

        var layout = new Grid
        {
            Padding = new Thickness(18, 0, 10, 0),
            ColumnSpacing = 8,
        };
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        _root.Children.Add(layout);

        _line.Foreground = (Brush)Application.Current.Resources["TextPrimaryBrush"];
        _line.FontWeight = Microsoft.UI.Text.FontWeights.SemiBold;
        _line.TextAlignment = TextAlignment.Center;
        _line.TextWrapping = TextWrapping.NoWrap;
        _line.TextTrimming = TextTrimming.CharacterEllipsis;
        _line.MaxLines = 1;
        _line.VerticalAlignment = VerticalAlignment.Center;
        _line.IsHitTestVisible = false;
        layout.Children.Add(_line);

        _pinButton = Chip("\uE718", "置顶显示");
        _pinButton.Click += (_, _) =>
        {
            SetAlwaysOnTop(!_onTop);
            TopChanged?.Invoke(_onTop);
        };
        var minus = Chip("A−", "缩小歌词");
        minus.Click += (_, _) => AdjustScale(-0.1);
        var plus = Chip("A+", "放大歌词");
        plus.Click += (_, _) => AdjustScale(0.1);
        var close = Chip("\uE711", "关闭桌面歌词");
        close.Click += (_, _) => CloseRequested?.Invoke();

        _bar.Orientation = Orientation.Horizontal;
        _bar.Spacing = 3;
        _bar.VerticalAlignment = VerticalAlignment.Center;
        _bar.Opacity = 0;
        _bar.Children.Add(_pinButton);
        _bar.Children.Add(minus);
        _bar.Children.Add(plus);
        _bar.Children.Add(close);
        Grid.SetColumn(_bar, 1);
        layout.Children.Add(_bar);

        _root.PointerEntered += (_, _) => _bar.Opacity = 1;
        _root.PointerExited += (_, _) => _bar.Opacity = 0;
        _root.PointerPressed += OnPressed;
        _root.PointerMoved += OnMoved;
        _root.PointerReleased += OnReleased;
        _root.PointerCaptureLost += (_, _) => EndDrag(save: true);

        ConfigureChrome(onTop, light);
        PlaceWindow(x, y, width, height);
        SetAlwaysOnTop(onTop);
        Recompute();
    }

    public void ShowNoActivate()
    {
        try
        {
            AppWindow.Show(false);
        }
        catch
        {
            AppWindow.Show();
        }
    }

    public void HidePill() => AppWindow.Hide();

    public void SetLyric(string? text)
    {
        _lyric = text?.Trim() ?? "";
        Recompute();
    }

    public void SetTrack(string? title, string? artist, bool playing)
    {
        _title = title?.Trim() ?? "";
        _artist = artist?.Trim() ?? "";
        _playing = playing;
        Recompute();
    }

    public void SetPlaying(bool playing)
    {
        if (_playing == playing) return;
        _playing = playing;
        Recompute();
    }

    public void ApplyTheme(bool light)
    {
        _root.RequestedTheme = light ? ElementTheme.Light : ElementTheme.Dark;
        _tint.Color = light
            ? Color.FromArgb(0x8C, 0xFF, 0xFF, 0xFF)
            : Color.FromArgb(0x74, 0x0B, 0x0F, 0x16);
        if (!_backdropOk)
        {
            _root.Background = new SolidColorBrush(light
                ? Color.FromArgb(0xFF, 0xF3, 0xF5, 0xFA)
                : Color.FromArgb(0xFF, 0x0C, 0x10, 0x16));
        }
    }

    public void SetAlwaysOnTop(bool onTop)
    {
        _onTop = onTop;
        try
        {
            if (AppWindow.Presenter is OverlappedPresenter presenter)
                presenter.IsAlwaysOnTop = onTop;
        }
        catch { }
        _pinButton.Foreground = onTop
            ? (Brush)Application.Current.Resources["AccentBrush"]
            : (Brush)Application.Current.Resources["TextSecondaryBrush"];
        ToolTipService.SetToolTip(_pinButton, onTop ? "取消置顶" : "置顶显示");
    }

    private Button Chip(string glyph, string tip)
    {
        var button = new Button
        {
            Style = (Style)Application.Current.Resources["IconButtonStyle"],
            Width = 26,
            Height = 26,
            CornerRadius = new CornerRadius(8),
            FontSize = 12,
            Content = glyph.Length == 1
                ? new FontIcon { Glyph = glyph, FontSize = 11 }
                : new TextBlock
                {
                    Text = glyph,
                    FontFamily = new FontFamily("Segoe UI"),
                    FontSize = 11,
                },
        };
        ToolTipService.SetToolTip(button, tip);
        return button;
    }

    private void Recompute()
    {
        if (!string.IsNullOrWhiteSpace(_lyric))
        {
            _line.Text = _lyric;
            _line.FontSize = BaseFont * _scale;
            _line.FontWeight = Microsoft.UI.Text.FontWeights.SemiBold;
            _line.Opacity = _playing ? 1 : 0.62;
        }
        else
        {
            _line.Text = string.IsNullOrWhiteSpace(_title)
                ? "没有播放"
                : (_artist.Length > 0 ? $"{_title}  ·  {_artist}" : _title);
            _line.FontSize = 15.5 * _scale;
            _line.FontWeight = Microsoft.UI.Text.FontWeights.Normal;
            _line.Opacity = 0.62;
        }
    }

    private void AdjustScale(double delta)
    {
        var next = Math.Clamp(Math.Round(_scale + delta, 2), MinScale, MaxScale);
        if (Math.Abs(next - _scale) < 0.001) return;
        _scale = next;
        Recompute();
        ScaleChanged?.Invoke(_scale);
    }

    private void ConfigureChrome(bool onTop, bool light)
    {
        try
        {
            if (AppWindow.Presenter is OverlappedPresenter presenter)
            {
                presenter.SetBorderAndTitleBar(false, false);
                presenter.IsResizable = false;
                presenter.IsMaximizable = false;
                presenter.IsMinimizable = false;
                presenter.IsAlwaysOnTop = onTop;
            }
            AppWindow.IsShownInSwitchers = false;
        }
        catch { }

        try
        {
            SystemBackdrop = new DesktopAcrylicBackdrop();
            _backdropOk = true;
        }
        catch
        {
            _backdropOk = false;
            ApplyTheme(light);
        }

        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            var round = 2;
            DwmSetWindowAttribute(hwnd, 33, ref round, sizeof(int));
        }
        catch { }
    }

    private void PlaceWindow(int? x, int? y, int width, int height)
    {
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            var scale = GetDpiForWindow(hwnd) / 96.0;
            if (scale <= 0 || double.IsNaN(scale)) scale = 1.0;

            var hasPos = x is int && y is int;
            var area = hasPos
                ? DisplayArea.GetFromPoint(
                    new PointInt32((int)Math.Round(x!.Value * scale), (int)Math.Round(y!.Value * scale)),
                    DisplayAreaFallback.Nearest)
                : DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
            area ??= DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary);
            var work = area.WorkArea;

            var wDip = Math.Clamp(width > 0 ? width : 460, 260, Math.Max(260, work.Width / scale));
            var hDip = Math.Clamp(height > 0 ? height : 68, 44, Math.Max(44, work.Height / scale));
            var w = (int)Math.Round(wDip * scale);
            var h = (int)Math.Round(hDip * scale);

            int px, py;
            if (hasPos)
            {
                px = (int)Math.Round(x!.Value * scale);
                py = (int)Math.Round(y!.Value * scale);
                // 旧位置可能来自别的分辨率/缩放（Qt 时代甚至换过 125%），
                // 整条落在工作区外就回到底部居中，而不是贴死在屏幕边缘。
                if (px < work.X || py < work.Y ||
                    px + w > work.X + work.Width || py + h > work.Y + work.Height)
                {
                    px = work.X + (work.Width - w) / 2;
                    py = work.Y + work.Height - h - (int)Math.Round(72 * scale);
                }
            }
            else
            {
                px = work.X + (work.Width - w) / 2;
                py = work.Y + work.Height - h - (int)Math.Round(72 * scale);
            }

            AppWindow.MoveAndResize(new RectInt32(px, py, w, h));
        }
        catch { }
    }

    private void OnPressed(object sender, PointerRoutedEventArgs e)
    {
        var point = e.GetCurrentPoint(_root);
        if (!point.Properties.IsLeftButtonPressed) return;
        _dragging = true;
        _dragOrigin = point.Position;
        _dragWindowOrigin = AppWindow.Position;
        _root.CapturePointer(e.Pointer);
        e.Handled = true;
    }

    private void OnMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        var point = e.GetCurrentPoint(_root);
        if (!point.Properties.IsLeftButtonPressed)
        {
            EndDrag(save: true);
            return;
        }
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var scale = GetDpiForWindow(hwnd) / 96.0;
        if (scale <= 0 || double.IsNaN(scale)) scale = 1.0;
        var dx = (int)Math.Round((point.Position.X - _dragOrigin.X) * scale);
        var dy = (int)Math.Round((point.Position.Y - _dragOrigin.Y) * scale);
        AppWindow.Move(new PointInt32(_dragWindowOrigin.X + dx, _dragWindowOrigin.Y + dy));
        e.Handled = true;
    }

    private void OnReleased(object sender, PointerRoutedEventArgs e) => EndDrag(save: true);

    private void EndDrag(bool save)
    {
        if (!_dragging) return;
        _dragging = false;
        _root.ReleasePointerCaptures();
        if (!save) return;
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            var scale = GetDpiForWindow(hwnd) / 96.0;
            if (scale <= 0 || double.IsNaN(scale)) scale = 1.0;
            var pos = AppWindow.Position;
            var size = AppWindow.Size;
            Moved?.Invoke(new RectInt32(
                (int)Math.Round(pos.X / scale), (int)Math.Round(pos.Y / scale),
                (int)Math.Round(size.Width / scale), (int)Math.Round(size.Height / scale)));
        }
        catch { }
    }
}
