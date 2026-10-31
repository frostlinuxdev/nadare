# SPDX-License-Identifier: GPL-3.0-or-later
"""Theme invariants: a cool blue palette, blue-only placeholders, WCAG contrast.

The launcher is deliberately blue-and-black.  These tests make that a hard
contract: no green or teal may sneak into the stylesheet or the procedural
placeholder art, and the body text must stay readable on the dark canvas.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtGui import QColor  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import theme
from nadare.gui.theme import _seed_colors

#: A hue is "green/teal" when it lands in this band and is at all saturated.
GREEN_MIN_DEG = 75.0
GREEN_MAX_DEG = 185.0
SATURATION_FLOOR = 0.15

HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")
TOKEN_RE = re.compile(r"#[0-9a-fA-F]{6}")
RGB_TRIPLE_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def _rgb_channels(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _hue_saturation(value: str) -> tuple[float, float]:
    r, g, b = (channel / 255 for channel in _rgb_channels(value))
    hue, saturation, _value = colorsys.rgb_to_hsv(r, g, b)
    return hue * 360.0, saturation


def _literal_colours() -> set[str]:
    source = Path(theme.__file__).read_text(encoding="utf-8")
    return {"#" + match for match in HEX_RE.findall(source)}


def _token_colours() -> set[str]:
    return {
        value
        for value in vars(theme).values()
        if isinstance(value, str) and TOKEN_RE.fullmatch(value)
    }


def _stylesheet_rgb_triples() -> set[str]:
    source = Path(theme.__file__).read_text(encoding="utf-8")
    return {
        f"#{int(r):02x}{int(g):02x}{int(b):02x}"
        for r, g, b in RGB_TRIPLE_RE.findall(source)
    }


def test_stylesheet_has_no_green_or_teal():
    """Every ``#rrggbb`` literal and palette token must be non-green/teal."""
    offenders: list[tuple[str, float, float]] = []
    values = _literal_colours() | _token_colours() | _stylesheet_rgb_triples()
    for value in sorted(values):
        hue, saturation = _hue_saturation(value)
        if saturation > SATURATION_FLOOR and GREEN_MIN_DEG <= hue <= GREEN_MAX_DEG:
            offenders.append((value, round(hue, 1), round(saturation, 3)))
    assert not offenders, f"green/teal colours found: {offenders}"


def test_palette_tokens_are_the_documented_cool_set():
    """The core background/accent tokens match the blue-and-black spec."""
    assert theme.BG == "#07090d"
    assert theme.PANEL == "#0c0f15"
    assert theme.OK == theme.ACCENT


def test_placeholder_seed_colours_stay_blue():
    """Procedural art for 200 seeds must stay in the blue band (195-240°)."""
    offenders: list[tuple[int, int, float, float]] = []
    for index in range(200):
        for channel, colour in enumerate(_seed_colors(f"profile-{index}")):
            saturation = colour.saturationF()
            if saturation < SATURATION_FLOOR:
                continue
            hue = colour.hueF() * 360.0
            if not 195.0 <= hue <= 240.0:
                offenders.append((index, channel, round(hue, 1), round(saturation, 3)))
    assert not offenders, f"placeholder colours left the blue band: {offenders[:10]}"


def _relative_luminance(value: str) -> float:
    def linearise(channel: int) -> float:
        srgb = channel / 255.0
        if srgb <= 0.04045:
            return srgb / 12.92
        return ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb_channels(value)
    return 0.2126 * linearise(r) + 0.7152 * linearise(g) + 0.0722 * linearise(b)


def _contrast(first: str, second: str) -> float:
    lighter = max(_relative_luminance(first), _relative_luminance(second))
    darker = min(_relative_luminance(first), _relative_luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def test_body_text_meets_wcag_aa_and_muted_meets_aa_large():
    assert _contrast(theme.TEXT, theme.BG) >= 4.5
    assert _contrast(theme.TEXT, theme.PANEL) >= 4.5
    assert _contrast(theme.MUTED, theme.PANEL) >= 3.0


# ---- a cover standing in for a hero banner ---------------------------------
@pytest.fixture(scope="module")
def banner_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _two_tone(width: int, height: int, top_fraction: float):
    from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ff0000"))
    painter = QPainter(image)
    painter.fillRect(0, 0, width, int(height * top_fraction), QColor("#0000ff"))
    painter.end()
    return QPixmap.fromImage(image)


def _centre(pixmap) -> tuple[int, int, int]:
    from PyQt6.QtGui import QColor

    image = pixmap.toImage()
    return QColor(image.pixel(image.width() // 2, image.height() // 2)).getRgb()[:3]


def test_a_wide_cover_banner_keeps_the_top_of_the_art(banner_app):
    # Top 40% blue, rest red. A centre crop of a 16:9 cover shows its middle, which is red; the
    # banner keeps the top, where logos and faces are, so its middle is still blue.
    banner = theme.banner_from_cover(_two_tone(1600, 900, 0.4), 830, 300)
    assert (banner.width(), banner.height()) == (830, 300)
    red, _, blue = _centre(banner)
    assert blue > 200 and red < 60


def test_a_portrait_cover_banner_shows_the_whole_poster_over_a_dim_blur(banner_app):
    from PyQt6.QtGui import QColor

    banner = theme.banner_from_cover(_two_tone(600, 900, 0.5), 830, 300)
    image = banner.toImage()
    # The poster's top half is blue and fills the banner height: its middle row is the seam,
    # so look at the top: poster centred, blur at the sides.
    top_middle = QColor(image.pixel(image.width() // 2, 5)).getRgb()[:3]
    side = QColor(image.pixel(40, 150)).getRgb()[:3]
    assert top_middle[2] > 200 and top_middle[0] < 60  # the poster, undimmed
    assert max(side) < 200  # the blurred backdrop is darkened, never full colour
