using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace SaltPlayer.Native.Services;

/// <summary>
/// 深/浅两套令牌。XAML 里全部用 StaticResource 引用同一批画刷对象，
/// 切主题时直接改画刷颜色，引用方不需要重新加载。
/// </summary>
internal static class ThemePalette
{
    private static readonly Dictionary<string, string> Dark = new()
    {
        ["TextPrimaryBrush"] = "#F4F5F8",
        ["TextSecondaryBrush"] = "#A7AEBC",
        ["TextTertiaryBrush"] = "#717A8B",
        ["TextDisabledBrush"] = "#4E5665",
        ["AccentBrush"] = "#F2A66F",
        ["AccentStrongBrush"] = "#FFB782",
        ["AccentOnBrush"] = "#1C1209",
        ["AccentSoftBrush"] = "#2EF2A66F",
        ["DangerBrush"] = "#FF8080",
        ["HairlineBrush"] = "#14FFFFFF",
        ["HairlineStrongBrush"] = "#24FFFFFF",
        ["NavFgBrush"] = "#A7AEBC",
        ["NavHoverBrush"] = "#0AFFFFFF",
        ["NavPressedBrush"] = "#12FFFFFF",
        ["NavSelectedBrush"] = "#11FFFFFF",
        ["RowHoverBrush"] = "#08FFFFFF",
        ["RowPressedBrush"] = "#10FFFFFF",
        ["RowSelectedBrush"] = "#14FFFFFF",
        ["RowPlayingBrush"] = "#12F2A66F",
        ["ControlFillBrush"] = "#0CFFFFFF",
        ["ControlFillHoverBrush"] = "#16FFFFFF",
        ["ControlStrokeBrush"] = "#1CFFFFFF",
        ["ControlBorderBrush"] = "#16FFFFFF",
        ["ScrimBrush"] = "#B3080A0E",
        ["PanelFallbackBrush"] = "#0B0E14",
        ["ArtFrameBrush"] = "#0CFFFFFF",
        ["ArtPlaceholderBrush"] = "#11161E",
        ["SidebarPanelBrush"] = "#E00E121A",
        ["BubbleBrush"] = "#E8151922",
        ["LyricsSurfaceBrush"] = "#FF090B10",
        ["BootOverlayBrush"] = "#F607090D",
        ["ToastBrush"] = "#F012161E",
        ["PlayButtonBrush"] = "#F4F5F8",
        ["PlayButtonFgBrush"] = "#0B0D11",
        ["SliderTrackFill"] = "#2BFFFFFF",
        ["SliderTrackFillPointerOver"] = "#38FFFFFF",
        ["SliderTrackFillPressed"] = "#38FFFFFF",
        ["SliderTrackValueFill"] = "#F2A66F",
        ["SliderTrackValueFillPointerOver"] = "#FFB782",
        ["SliderTrackValueFillPressed"] = "#FFB782",
        ["SliderThumbBackground"] = "#FFFFFF",
        ["SliderThumbBackgroundPointerOver"] = "#FFFFFF",
        ["SliderThumbBackgroundPressed"] = "#FFFFFF",
        ["ToggleTrackOffBrush"] = "#26FFFFFF",
        ["ToggleTrackOffStrokeBrush"] = "#3DFFFFFF",
        ["ToggleKnobBrush"] = "#FFFFFF",
        ["ToggleKnobStrokeBrush"] = "#00FFFFFF",
    };

    private static readonly Dictionary<string, string> Light = new()
    {
        ["TextPrimaryBrush"] = "#191C22",
        ["TextSecondaryBrush"] = "#565D6B",
        ["TextTertiaryBrush"] = "#7E8695",
        ["TextDisabledBrush"] = "#AEB5C1",
        ["AccentBrush"] = "#D97840",
        ["AccentStrongBrush"] = "#B95F2E",
        ["AccentOnBrush"] = "#FFF8F2",
        ["AccentSoftBrush"] = "#28D97840",
        ["DangerBrush"] = "#C63C3C",
        ["HairlineBrush"] = "#14000000",
        ["HairlineStrongBrush"] = "#24000000",
        ["NavFgBrush"] = "#565D6B",
        ["NavHoverBrush"] = "#08000000",
        ["NavPressedBrush"] = "#12000000",
        ["NavSelectedBrush"] = "#0F000000",
        ["RowHoverBrush"] = "#05000000",
        ["RowPressedBrush"] = "#0C000000",
        ["RowSelectedBrush"] = "#12000000",
        ["RowPlayingBrush"] = "#1AD97840",
        ["ControlFillBrush"] = "#0C000000",
        ["ControlFillHoverBrush"] = "#16000000",
        ["ControlStrokeBrush"] = "#1C000000",
        ["ControlBorderBrush"] = "#18000000",
        ["ScrimBrush"] = "#B3E9EDF3",
        ["PanelFallbackBrush"] = "#F2F4F8",
        ["ArtFrameBrush"] = "#0A000000",
        ["ArtPlaceholderBrush"] = "#E3E8F0",
        ["SidebarPanelBrush"] = "#D9FFFFFF",
        ["BubbleBrush"] = "#F2FFFFFF",
        ["LyricsSurfaceBrush"] = "#FFF6F8FB",
        ["BootOverlayBrush"] = "#F7F8FB",
        ["ToastBrush"] = "#F2FFFFFF",
        ["PlayButtonBrush"] = "#171A20",
        ["PlayButtonFgBrush"] = "#FFFFFF",
        // 浅色空轨道必须是深色半透明，否则白底上整条滑杆消失；滑块用近黑，
        // 在轨道和橙色进度上都看得见（系统模板的滑块没有描边/阴影可借）。
        ["SliderTrackFill"] = "#24000000",
        ["SliderTrackFillPointerOver"] = "#33000000",
        ["SliderTrackFillPressed"] = "#3D000000",
        ["SliderTrackValueFill"] = "#D97840",
        ["SliderTrackValueFillPointerOver"] = "#C96B36",
        ["SliderTrackValueFillPressed"] = "#B95F2E",
        ["SliderThumbBackground"] = "#2E333C",
        ["SliderThumbBackgroundPointerOver"] = "#191C22",
        ["SliderThumbBackgroundPressed"] = "#191C22",
        // 浅色 Off 轨道用 12% 黑才看得见；白色旋钮加一圈浅描边，避免和轨道糊在一起。
        ["ToggleTrackOffBrush"] = "#1F000000",
        ["ToggleTrackOffStrokeBrush"] = "#2E000000",
        ["ToggleKnobBrush"] = "#FFFFFF",
        ["ToggleKnobStrokeBrush"] = "#24000000",
    };

    private static readonly Dictionary<string, string[]> DarkGradients = new()
    {
        ["WindowBackgroundBrush"] = new[] { "#0D1017", "#090B10", "#06070A" },
        ["GlassBorderBrush"] = new[] { "#3AFFFFFF", "#14FFFFFF", "#0AFFFFFF", "#1FFFFFFF" },
        ["GlassSheenBrush"] = new[] { "#14FFFFFF", "#00FFFFFF" },
        ["HeroWashBrush"] = new[] { "#26F2A66F", "#0E9A7BE0", "#00000000" },
        ["LyricWashBrush"] = new[] { "#1CF2A66F", "#0A9A7BE0", "#00000000" },
        ["WashABrush"] = new[] { "#382A5F58", "#152A5F58", "#002A5F58" },
        ["WashBBrush"] = new[] { "#2A2C3E70", "#0E2C3E70", "#002C3E70" },
        ["WashCBrush"] = new[] { "#26533145", "#0C533145", "#00533145" },
    };

    private static readonly Dictionary<string, string[]> LightGradients = new()
    {
        ["WindowBackgroundBrush"] = new[] { "#F2F4F8", "#EAEEF5", "#E5EAF2" },
        // 浅色玻璃：左上白色高光 + 右下深色描边，才有厚度而不是一片白。
        ["GlassBorderBrush"] = new[] { "#66FFFFFF", "#1C000000", "#0E000000", "#26000000" },
        ["GlassSheenBrush"] = new[] { "#99FFFFFF", "#00FFFFFF" },
        ["HeroWashBrush"] = new[] { "#33D97840", "#1A6C86C8", "#00000000" },
        ["LyricWashBrush"] = new[] { "#24D97840", "#107286C8", "#00000000" },
        ["WashABrush"] = new[] { "#1C5E8C7E", "#0E5E8C7E", "#005E8C7E" },
        ["WashBBrush"] = new[] { "#166B7FB8", "#0A6B7FB8", "#006B7FB8" },
        ["WashCBrush"] = new[] { "#146E4E58", "#086E4E58", "#006E4E58" },
    };

    private static readonly Dictionary<string, (string Tint, double Opacity, double Luminosity, string Fallback)> DarkAcrylic = new()
    {
        ["GlassBrush"] = ("#0B0F16", 0.34, 0.52, "#0C1016"),
        ["GlassRaisedBrush"] = ("#10151E", 0.46, 0.62, "#0F141C"),
    };

    private static readonly Dictionary<string, (string Tint, double Opacity, double Luminosity, string Fallback)> LightAcrylic = new()
    {
        ["GlassBrush"] = ("#FFFFFF", 0.40, 0.85, "#F3F5FA"),
        ["GlassRaisedBrush"] = ("#FFFFFF", 0.56, 0.92, "#F8FAFD"),
    };

    public static void Apply(bool light)
    {
        var res = Application.Current.Resources;
        foreach (var (key, value) in light ? Light : Dark)
        {
            if (res.TryGetValue(key, out var brush) && brush is SolidColorBrush solid)
                solid.Color = Parse(value);
        }
        foreach (var (key, values) in light ? LightGradients : DarkGradients)
        {
            if (!res.TryGetValue(key, out var brush) || brush is not GradientBrush gradient) continue;
            var stops = gradient.GradientStops;
            for (var i = 0; i < stops.Count && i < values.Length; i++)
                stops[i].Color = Parse(values[i]);
        }
        foreach (var (key, spec) in light ? LightAcrylic : DarkAcrylic)
        {
            if (!res.TryGetValue(key, out var brush) || brush is not AcrylicBrush acrylic) continue;
            acrylic.TintColor = Parse(spec.Tint);
            acrylic.TintOpacity = spec.Opacity;
            acrylic.TintLuminosityOpacity = spec.Luminosity;
            acrylic.FallbackColor = Parse(spec.Fallback);
        }
    }

    public static Color Parse(string hex)
    {
        var text = hex.TrimStart('#');
        if (text.Length == 6) text = "FF" + text;
        var value = Convert.ToUInt32(text, 16);
        return Color.FromArgb(
            (byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value);
    }
}
