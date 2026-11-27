# SPDX-License-Identifier: GPL-3.0-or-later
"""#1708: the hover Play button sits bottom right on both library tile kinds.

Every library tile (a grid card, a favourite, a Continue playing tile) shares one look, so
the button's corner and the room it takes from the name are the same on both kinds.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import QApplication

from nadare.gui import library, theme

#: Long enough that the narrower name slot has to elide it at any scale.
LONG_TITLE = "Arknights: Endfield and the very long subtitle of a game"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _tile(kind: str, title: str = LONG_TITLE):
    if kind == "card":
        return library.GameCard("a", title)
    return library.RecentTile("a", title, "2 hours ago")


def _name(widget):
    return widget.title_label if isinstance(widget, library.GameCard) else widget.name


def _hover(widget) -> None:
    """Show the tile's Play button the way a mouse entering the tile does."""
    widget.set_state("installed")
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Enter))
    QApplication.processEvents()


@pytest.mark.parametrize("kind", ["card", "tile"])
@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_the_play_button_sits_in_the_bottom_right_corner(app, monkeypatch, kind, scale):
    monkeypatch.setattr(theme, "UI_SCALE", scale)
    widget = _tile(kind)
    widget.resize_tile(round(300 * scale))
    play = widget.play_button.geometry()
    inset = theme.scaled(8)
    assert play.width() == play.height() == theme.scaled(32)
    # The scaled 8 px inset from the right and the bottom edges, as on every tile.
    assert widget.width() - play.right() - 1 == inset
    assert widget.height() - play.bottom() - 1 == inset


@pytest.mark.parametrize("kind", ["card", "tile"])
def test_the_name_ends_before_the_play_button_when_it_shows(app, kind):
    widget = _tile(kind)
    widget.resize_tile(300)
    name = _name(widget)
    resting = name.geometry()
    resting_text = name.text()
    assert widget.play_button.isHidden()
    assert resting.width() == widget.width() - theme.scaled(24)  # the whole line while it can

    _hover(widget)
    play = widget.play_button.geometry()
    assert not widget.play_button.isHidden()
    shown = name.geometry()
    # The button covers no part of the name, and the name keeps its line.
    assert shown.right() < play.left()
    assert play.left() - shown.right() - 1 == theme.scaled(8)  # the tile's own margin
    assert shown.x() == resting.x()
    assert shown.bottom() == resting.bottom()
    assert shown.width() < resting.width()
    # The elide width makes room: the narrower slot cuts the name sooner.
    assert len(name.text()) < len(resting_text)
    assert name.text().endswith("…")
    assert QFontMetrics(name.font()).horizontalAdvance(name.text()) <= shown.width()

    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert widget.play_button.isHidden()
    assert name.geometry() == resting
    assert name.text() == resting_text


@pytest.mark.parametrize("kind", ["card", "tile"])
def test_a_short_name_is_not_cut_when_the_button_shows(app, kind):
    widget = _tile(kind, "Arknights")
    widget.resize_tile(300)
    name = _name(widget)
    _hover(widget)
    assert not widget.play_button.isHidden()
    assert name.text() == "Arknights"
    assert name.geometry().right() < widget.play_button.geometry().left()
