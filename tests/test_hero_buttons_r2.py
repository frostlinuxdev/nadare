# SPDX-License-Identifier: GPL-3.0-or-later
"""Buttons over the hero art stay readable (user sim r2v 015).

The stop "x" next to PLAYING was a faint outline on the art, and disabled Update /
Check / Repair buttons were see-through, so their text sat on the picture.
"""
import pytest
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton

from nadare.gui import theme


def luminance(c: QColor) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture
def hero():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    frame = QFrame()
    frame.setObjectName("Hero")
    # worst case: bright, light art right behind the buttons
    frame.setStyleSheet("QFrame#Hero { background: #e8eef5; }")
    row = QHBoxLayout(frame)
    stop = QPushButton("✕")
    stop.setObjectName("StopButton")
    repair = QPushButton("Repair files")
    repair.setObjectName("GhostButton")
    repair.setEnabled(False)
    row.addWidget(stop)
    row.addWidget(repair)
    frame.resize(400, 80)
    frame.show()
    app.processEvents()
    yield frame, stop, repair
    frame.close()


def colors(widget) -> tuple[QColor, QColor]:
    """(background, the pixel most unlike it) of ``widget`` as drawn over the hero art."""
    img: QImage = widget.parentWidget().grab(widget.geometry()).toImage()
    bg = img.pixelColor(3, img.height() // 2)
    best = bg
    for x in range(img.width()):
        for y in range(img.height()):
            c = img.pixelColor(x, y)
            if contrast(c, bg) > contrast(best, bg):
                best = c
    return bg, best


def test_the_stop_button_is_solid_over_light_art(hero):
    _, stop, _ = hero
    bg, ink = colors(stop)
    assert contrast(bg, QColor("#e8eef5")) >= 3.0, "stop button background blends into the art"
    assert contrast(ink, bg) >= 4.5, "the x is not readable on its own background"


def test_disabled_buttons_over_art_have_a_backing(hero):
    _, _, repair = hero
    bg, ink = colors(repair)
    assert contrast(bg, QColor("#e8eef5")) >= 3.0, "disabled button is see-through over the art"
    assert contrast(ink, bg) >= 3.0, "disabled label unreadable"


def test_the_game_page_uses_the_stop_style():
    app = QApplication.instance() or QApplication([])
    from nadare.gui.game_view import GameView

    view = GameView()
    assert view.stop_button.objectName() == "StopButton"
    view.deleteLater()
    app.processEvents()
