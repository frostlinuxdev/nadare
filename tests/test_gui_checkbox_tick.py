# SPDX-License-Identifier: GPL-3.0-or-later
"""A ticked checkbox shows a tick, not just an accent-coloured square."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QCheckBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import theme

_INK = (0x08, 0x13, 0x1F)
_ACCENT = (0x3D, 0x8B, 0xFD)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    theme.apply_theme(app)
    yield app


def _near(colour, target) -> bool:
    return all(abs(a - b) <= 24 for a, b in zip(colour, target, strict=True))


def _tick_pixels(box: QCheckBox) -> tuple[int, int]:
    """(accent pixels, ink pixels strictly inside the accent-filled indicator)."""
    box.show()
    QApplication.processEvents()
    image = box.grab().toImage()
    rgb = {}
    for y in range(image.height()):
        for x in range(min(30, image.width())):
            c = image.pixelColor(x, y)
            rgb[(x, y)] = (c.red(), c.green(), c.blue())
    accent = [p for p, c in rgb.items() if _near(c, _ACCENT)]
    if not accent:
        return 0, 0
    x0, x1 = min(p[0] for p in accent), max(p[0] for p in accent)
    y0, y1 = min(p[1] for p in accent), max(p[1] for p in accent)
    ink = [
        p
        for p, c in rgb.items()
        if x0 + 3 <= p[0] <= x1 - 3 and y0 + 3 <= p[1] <= y1 - 3 and _near(c, _INK)
    ]
    return len(accent), len(ink)


def test_the_tick_asset_ships_with_the_package():
    assert theme._CHECK_ICON.is_file()
    assert "assets/icons/*" in (theme._CHECK_ICON.parents[4] / "pyproject.toml").read_text()


def test_checked_box_has_a_tick_and_unchecked_does_not(qapp):
    off = QCheckBox("Remember me on this device")
    on = QCheckBox("Remember me on this device")
    on.setChecked(True)
    assert _tick_pixels(off) == (0, 0), "an unchecked box must not be accent-filled"
    fill, tick = _tick_pixels(on)
    assert fill > 100, "the checked box is not accent-filled"
    assert tick > 8, "no tick drawn inside the checked indicator"
    off.deleteLater()
    on.deleteLater()
