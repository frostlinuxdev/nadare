# SPDX-License-Identifier: GPL-3.0-or-later
"""A game's exit code after play is not automatically a failed operation."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import game_view as game_view_mod
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("sample", {"title": "Sample"})
    yield page
    page.deleteLater()
    qapp.processEvents()


@pytest.fixture
def warnings(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        game_view_mod.QMessageBox,
        "warning",
        lambda _parent, title, message: seen.append((title, message)),
    )
    return seen


def _finish_after(view, monkeypatch, operation: str, exit_code: int, seconds: float) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(game_view_mod.time, "time", lambda: now[0])
    view.set_busy(operation)
    now[0] += seconds
    view.finish(operation, exit_code)


def test_zero_after_a_long_session_is_normal(view, monkeypatch, warnings):
    _finish_after(view, monkeypatch, "launch", 0, 600)
    assert "game exited normally" in view.console.toPlainText()
    assert warnings == []


def test_nonzero_after_a_long_session_is_not_an_error(view, monkeypatch, warnings):
    _finish_after(view, monkeypatch, "launch", 1, 600)
    log = view.console.toPlainText()
    assert "Game closed (exit code 1)" in log
    assert "did not finish" not in log
    assert warnings == []


@pytest.mark.parametrize("exit_code", [143, 137, 130, -15])
def test_stopped_by_a_signal_is_not_an_error(view, monkeypatch, warnings, exit_code):
    _finish_after(view, monkeypatch, "launch", exit_code, 2)
    log = view.console.toPlainText()
    assert f"Game closed (exit code {exit_code})" in log
    assert warnings == []


def test_nonzero_soon_after_launch_is_a_failed_launch(view, monkeypatch, warnings):
    _finish_after(view, monkeypatch, "launch", 1, 2)
    assert warnings == [
        (
            "Operation failed",
            "Launching did not finish (exit code 1). Open the logs for details.",
        )
    ]


def test_install_nonzero_is_still_a_failure(view, monkeypatch, warnings):
    _finish_after(view, monkeypatch, "install", 1, 600)
    assert warnings == [
        (
            "Operation failed",
            "Installing did not finish (exit code 1). Open the logs for details.",
        )
    ]


def test_an_unknown_launch_start_stays_a_failed_launch(view, monkeypatch, warnings):
    view.finish("launch", 1)
    assert len(warnings) == 1
    assert warnings[0][0] == "Operation failed"


def test_long_preparation_does_not_make_a_quick_crash_look_like_play(
    view, monkeypatch, warnings
):
    """Runner download / prefix setup before the game starts is not play time."""
    from nadare.core.events import Event

    now = [1_000_000.0]
    monkeypatch.setattr(game_view_mod.time, "time", lambda: now[0])
    view.set_busy("launch")
    now[0] += 600  # ten minutes of preparation
    view.append_event(Event("step", "Proton: launching game.exe"))
    now[0] += 3  # the game dies three seconds after it started
    view.finish("launch", 1)
    assert warnings, "a crash right after start must still be reported"


def test_a_real_session_after_the_launch_step_is_normal(view, monkeypatch, warnings):
    from nadare.core.events import Event

    now = [1_000_000.0]
    monkeypatch.setattr(game_view_mod.time, "time", lambda: now[0])
    view.set_busy("launch")
    now[0] += 600
    view.append_event(Event("step", "Proton: launching game.exe"))
    now[0] += 3600
    view.finish("launch", 1)
    assert warnings == []
    assert "Game closed (exit code 1)" in view.console.toPlainText()


@pytest.mark.parametrize("code", [-11, -7, -4, -6])
def test_a_crash_signal_right_after_start_is_still_a_failure(view, monkeypatch, warnings, code):
    """SIGSEGV/SIGBUS/SIGILL/SIGABRT are crashes, not 'the game was stopped'."""
    _finish_after(view, monkeypatch, "launch", code, 2)
    assert warnings, f"exit {code} two seconds after start must be reported"


@pytest.mark.parametrize("code", [-2, -9, -15])
def test_stop_signals_are_a_normal_close(view, monkeypatch, warnings, code):
    _finish_after(view, monkeypatch, "launch", code, 2)
    assert warnings == []
