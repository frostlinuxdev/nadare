# SPDX-License-Identifier: GPL-3.0-or-later
"""#1031: a game killed by a crash signal says so, even after a long session.

Exits were classified by elapsed time only: after 30 s any non-zero exit read "Game closed (exit
code -11)" in the console, so a segfault mid-game looked like a normal quit. A crash signal
(SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE, raw or shell-encoded) is now reported; STOP and an
ordinary non-zero quit stay normal.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.game_view import GameView


@pytest.fixture
def view():
    app = QApplication.instance() or QApplication([])
    page = GameView()
    page.set_profile("g", {"backend": "direct-pgr", "title": "G"})
    shown: list[tuple[str, str]] = []
    def choice(title, message, can_retry):
        shown.append((title, message))
        return "close"

    page._failure_choice = choice
    page.shown = shown
    yield page
    page.deleteLater()
    app.processEvents()


def _played(view, seconds: float) -> None:
    view.set_busy("launch")
    view._last_step_at = time.time() - seconds


@pytest.mark.parametrize("code", [-11, -6, -7, -4, -8, 139, 134, 135, 132, 136])
def test_a_crash_signal_after_a_long_session_is_reported(view, code):
    _played(view, 600)
    view.finish("launch", code)
    assert view.shown, "the crash was not reported"
    title, message = view.shown[0]
    assert "crash" in (title + message).lower()


@pytest.mark.parametrize("code", [1, 3, 255, -15, -9, 143, 137, 130])
def test_an_ordinary_quit_or_a_stop_after_a_long_session_stays_quiet(view, code):
    _played(view, 600)
    view.finish("launch", code)
    assert view.shown == []


def test_a_crash_while_stopping_is_not_reported(view):
    _played(view, 600)
    view._busy_op = "stop"
    view.finish("launch", -11)
    assert view.shown == []


def test_a_quick_failed_launch_is_still_a_failed_launch(view):
    _played(view, 2)
    view.finish("launch", 1)
    assert view.shown and "did not finish" in view.shown[0][1]
