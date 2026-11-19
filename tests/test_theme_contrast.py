"""Check actual palette color pairs, not screenshot/whole-app accessibility certification."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PALETTE = ROOT / "native/SaltPlayer.Native/Services/ThemePalette.cs"


def luminance(value):
    channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))


def contrast(a, b):
    x, y = sorted((luminance(a), luminance(b)))
    return (y + .05) / (x + .05)


class ThemeContrastTests(unittest.TestCase):
    def test_meter_known_answers(self):
        self.assertAlmostEqual(contrast("#000000", "#FFFFFF"), 21)
        self.assertAlmostEqual(contrast("#FFFFFF", "#FFFFFF"), 1)
        self.assertLess(contrast("#FFF8F2", "#D97840"), 3.1)

    def test_button_templates_apply_their_declared_padding(self):
        root = ET.parse(ROOT / "native/SaltPlayer.Native/App.xaml").getroot()
        x = "{http://schemas.microsoft.com/winfx/2006/xaml}"
        ui = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"
        styles = {s.get(x + "Key"): s for s in root.iter(ui + "Style")}
        for key in ("PrimaryButtonStyle", "GhostButtonStyle", "LinkButtonStyle"):
            with self.subTest(style=key):
                grids = list(styles[key].iter(ui + "Grid"))
                self.assertTrue(grids, "template grid is missing")
                self.assertEqual(grids[0].get("Padding"), "{TemplateBinding Padding}",
                                 "declaring Button.Padding is ineffective if the template ignores it")

    def test_actual_opaque_text_palette_pairs(self):
        text = PALETTE.read_text(encoding="utf-8-sig")
        for theme in ("Dark", "Light"):
            marker = f'Dictionary<string, string> {theme} = new()'
            self.assertEqual(text.count(marker), 1)
            body = text.split(marker, 1)[1].split("};", 1)[0]
            colors = dict(re.findall(r'\["([^"]+)"\]\s*=\s*"(#[0-9a-fA-F]{6})"', body))
            for fg, bg in (("AccentOnBrush", "AccentBrush"), ("AccentOnBrush", "AccentStrongBrush"),
                           ("TextPrimaryBrush", "PanelFallbackBrush"),
                           ("TextSecondaryBrush", "PanelFallbackBrush"),
                           ("TextTertiaryBrush", "PanelFallbackBrush")):
                with self.subTest(theme=theme, foreground=fg, background=bg):
                    self.assertIn(fg, colors)
                    self.assertIn(bg, colors)
                    self.assertGreaterEqual(contrast(colors[fg], colors[bg]), 4.5,
                                            f"{theme} {fg}/{bg}: {contrast(colors[fg], colors[bg]):.3f}:1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
