# SPDX-License-Identifier: GPL-3.0-or-later
"""The status pill under the badges stays on one line and elides (#1737).

The launch step ("Arknights: Endfield: launching Endfield.exe") used to wrap onto a
second line inside a pill that kept one line of height, so both lines were clipped.
The pill now keeps the whole status as its text, shows it elided with "…" to the
width the layout gives it, and puts the whole status in the tooltip.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.gui.game_view import GameView

LONG = "Arknights: Endfield: launching Endfield.exe"
LONGER = (
    "Arknights: Endfield: launching Endfield.exe, then waiting for the game window "
    "to show its first frame"
)
SHORT = "Ready to play"

#: The pill's stylesheet padding (3px 10px) and its 1px border, top and bottom.
_PILL_VERTICAL = 3 + 3 + 1 + 1


@pytest.fixture
def view(_one_qapplication):
    page = GameView()
    page.set_profile(
        "endfield", {"title": "Arknights: Endfield", "backend": "direct-arknights"}
    )
    yield page
    page._timer.stop()
    page.deleteLater()


def _settle(page: GameView, width: int, height: int = 800) -> None:
    page.resize(width, height)
    page.show()
    QApplication.processEvents()
    # A resize may need a second pass before the hero has its final width.
    QApplication.processEvents()


def _one_line_height(label) -> int:
    return label.fontMetrics().height() + _PILL_VERTICAL


def test_long_status_is_one_line_elided_and_keeps_the_whole_text_tooltip(view):
    view.set_status(Status("not_installed", message=LONG))
    _settle(view, 380)

    label = view.detail_label
    assert label.isVisible()
    # The layout gives the pill the narrow page's width; the pill must not wrap.
    assert not label.wordWrap()
    assert label.height() <= _one_line_height(label)
    # The logical text is the whole status; what is drawn is elided to the width.
    assert label.text() == LONG
    shown = label.display_text()
    assert shown.endswith("…")
    assert shown != LONG
    # It really fits inside the pill, not clipped by it.
    assert label.fontMetrics().horizontalAdvance(shown) <= label.contentsRect().width()
    # The whole status is still readable on hover.
    assert label.toolTip() == LONG


@pytest.mark.parametrize("status", [LONG, LONGER])
def test_a_longer_status_still_keeps_one_line(view, status):
    view.set_status(Status("not_installed", message=status))
    _settle(view, 380)

    label = view.detail_label
    assert label.isVisible()
    assert label.height() <= _one_line_height(label)
    assert label.text() == status
    assert label.display_text().endswith("…")
    assert label.toolTip() == status


def test_short_status_is_shown_whole_without_ellipsis_or_tooltip(view):
    view.set_status(Status("installed", message=SHORT))
    _settle(view, 380)

    label = view.detail_label
    assert label.isVisible()
    assert label.display_text() == SHORT
    assert "…" not in label.display_text()
    assert label.toolTip() == ""
    assert label.height() <= _one_line_height(label)


def test_the_elide_follows_resizes(view):
    view.set_status(Status("not_installed", message=LONG))
    # Wide: the whole status fits, no ellipsis.
    _settle(view, 1400)
    label = view.detail_label
    assert label.display_text() == LONG
    assert label.toolTip() == ""

    # Narrow: it elides.
    _settle(view, 380)
    assert label.display_text().endswith("…")
    assert label.display_text() != LONG
    assert label.toolTip() == LONG

    # Wide again: the whole status comes back.
    _settle(view, 1400)
    assert label.display_text() == LONG
    assert label.toolTip() == ""


def test_the_elide_follows_a_new_status(view):
    view.set_status(Status("installed", message=SHORT))
    _settle(view, 380)
    label = view.detail_label
    assert label.display_text() == SHORT
    assert label.toolTip() == ""

    view.set_status(Status("not_installed", message=LONGER))
    QApplication.processEvents()
    assert label.text() == LONGER
    assert label.display_text().endswith("…")
    assert label.toolTip() == LONGER

    view.set_status(Status("installed", message=SHORT))
    QApplication.processEvents()
    assert label.display_text() == SHORT
    assert label.toolTip() == ""
    # A short status makes the pill hug it again instead of keeping the wide box.
    assert label.width() <= label.sizeHint().width()
