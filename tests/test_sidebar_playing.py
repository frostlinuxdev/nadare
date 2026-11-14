# SPDX-License-Identifier: GPL-3.0-or-later
"""A running game says "Playing" in the sidebar, in the bright accent, without animation (#1136)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QPropertyAnimation
    from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui import theme
from nadare.gui.sidebar import ROW_HEIGHT, GameRow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _row(status):
    row = GameRow("g", "Game")
    row.set_state(status)
    return row


def _contrast(a: str, b: str) -> float:
    def luminance(colour: str) -> float:
        values = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_a_running_game_says_playing(qapp):
    row = _row(Status(state="running", running=True))
    assert not row.playing_label.isHidden()
    assert row.playing_label.text() == "Playing"
    assert row.dot.property("kind") == "running" and not row.dot.isHidden()
    assert "Playing" in row.accessibleDescription()


@pytest.mark.parametrize(
    "status",
    [None, Status(state="installed"), Status(state="installed", data={"update_available": True})],
)
def test_other_states_do_not(qapp, status):
    row = _row(status)
    assert row.playing_label.isHidden()
    assert "Playing" not in row.accessibleDescription()


def test_it_goes_away_when_the_game_stops(qapp):
    row = _row(Status(state="running", running=True))
    row.set_state(Status(state="installed"))
    assert row.playing_label.isHidden()
    assert row.dot.isHidden()


def test_live_is_readable_in_the_palette_and_used_for_the_dot_and_the_label():
    # Blue-and-black palette (no green, see test_theme): the bright accent, not the update amber.
    assert theme.LIVE == theme.ACCENT_HI and theme.LIVE != theme.WARN
    assert _contrast(theme.LIVE, theme.PANEL) >= 4.5  # the sidebar background
    css = theme.stylesheet()
    assert f'QLabel#GameRowDot[kind="running"] {{ background: {theme.LIVE}; }}' in css
    playing_rule = css.split("QLabel#GameRowPlaying", 1)[1][:80]
    assert theme.LIVE in playing_rule


def test_no_glow_or_animation(qapp):
    row = _row(Status(state="running", running=True))
    assert not row.findChildren(QPropertyAnimation)
    assert not isinstance(row.dot.graphicsEffect(), QGraphicsDropShadowEffect)


def test_the_two_lines_fit_the_row(qapp):
    row = _row(Status(state="running", running=True))
    row.resize(236, ROW_HEIGHT)
    row.show()
    qapp.processEvents()
    title, playing = row.title_label.geometry(), row.playing_label.geometry()
    assert playing.top() >= title.bottom() - 1
    assert playing.bottom() <= ROW_HEIGHT
    row.close()
