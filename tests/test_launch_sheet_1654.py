# SPDX-License-Identifier: GPL-3.0-or-later
"""In-window launch feedback follows the existing controller lifecycle."""
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui.main_window import MainWindow


@pytest.fixture
def window(tmp_path, qtbot, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.data.update(check_updates=False, check_notices=False)
    config.add_profile("g", backend="wine", title="Example Game")
    window = MainWindow(config)
    qtbot.addWidget(window)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)
    window.show()
    window.open_game("g")
    yield window
    window.hide()


def test_play_opens_embedded_sheet_and_cannot_dismiss(window, qtbot):
    window._on_game_action("launch")
    sheet = window._launch_sheet
    assert sheet.isVisible() and not sheet.isWindow()
    assert sheet.parent() is window
    assert sheet.geometry() == window.rect()
    assert sheet.title.text() == "Example Game"
    assert sheet.status.text() == "Preparing"
    assert sheet.elapsed.text() == "0 s"
    assert not window.centralWidget().isEnabled()
    assert [b.text() for b in sheet.buttons()] == ["Stop"]

    sheet.close()
    QTest.keyClick(sheet, Qt.Key.Key_Escape)
    sheet.stop.setFocus()
    QTest.keyClick(sheet.stop, Qt.Key.Key_Escape)
    QTest.mouseClick(sheet, Qt.MouseButton.LeftButton, pos=sheet.rect().topLeft())
    assert sheet.isVisible()

    calls = []
    window.controller.start = lambda *args, **kw: calls.append(args) or True
    qtbot.mouseClick(sheet.stop, Qt.MouseButton.LeftButton)
    assert calls == [("g", "stop", None)]
    assert not sheet.isVisible() and window.centralWidget().isEnabled()
    assert window._launch_sheet is None


@pytest.mark.parametrize("end", ["window", "exit", "failure", "cancel"])
def test_sheet_ends_with_launch_lifecycle(window, end):
    window._start("g", "launch")
    sheet = window._launch_sheet
    if end == "window":
        window._on_event("g", Event("window_seen"))
    elif end == "exit":
        window._on_busy("g", "launch", False)
    elif end == "failure":
        window._on_failed("g", "launch", "Could not start")
    else:
        window._on_cancelled("g", "launch")
    assert not sheet.isVisible()
    assert window._launch_sheet is None


def test_window_seen_is_feedback_not_a_console_line(window):
    window._start("g", "launch")
    sheet = window._launch_sheet
    window._on_busy("g", "launch", True)
    window._on_event("g", Event("window_seen"))
    assert window._operations.view("g").events == ()
    assert not sheet.isVisible()
    assert window._launch_sheet is None


def test_progress_timeout_and_hide(window, qtbot):
    window._start("g", "launch")
    sheet = window._launch_sheet
    sheet.started = float(int(sheet.started))  # whole seconds: started + N - started == N
    window._on_event("g", Event("step", "Starting Chaos Zero Nightmare…"))
    assert sheet.status.text() == "Starting Wine"
    window._on_event("g", Event("step", "launching Example.exe"))
    assert sheet.status.text() == "Starting Wine"
    window._on_event("g", Event("game_running"))
    assert sheet.status.text() == "Waiting for the window"
    window._on_event("g", Event("step", "launching a late helper"))
    assert sheet.status.text() == "Waiting for the window"
    sheet.tick(sheet.started + 2)
    assert sheet.status.text() == "Waiting for the window"
    assert sheet.elapsed.text() == "2 s"
    sheet.tick(sheet.started + 90)
    assert sheet.status.text() == "Still starting. Its own launcher or anti-cheat can take a while."
    assert [b.text() for b in sheet.buttons()] == ["Stop", "Hide"]
    window._on_event("g", Event("game_running"))
    assert sheet.status.text().startswith("Still starting.")
    qtbot.mouseClick(sheet.hide_button, Qt.MouseButton.LeftButton)
    assert not sheet.isVisible()
    assert window._launch_sheet is None


def test_hidden_main_window_never_shows_sheet(window):
    window.hide()
    window._start("g", "launch")
    assert window._launch_sheet is None
    window.show()
    window._start("g", "launch")
    sheet = window._launch_sheet
    window.hide()
    window.show()
    assert not sheet.isVisible()
    assert window._launch_sheet is None


def _running_launch(window):
    window._on_result("g", "status", Status("installed"))
    window._start("g", "launch")
    window._on_busy("g", "launch", True)
    window._on_event("g", Event("game_running"))


@pytest.mark.parametrize("ending", ["exit", "failure", "error"])
@pytest.mark.parametrize("previous", [None, "2026-01-01T00:00:00+00:00", ""])
def test_failed_launch_restores_history_and_clears_all_playing(window, ending, previous):
    import json

    if previous is None:
        window.config.profiles["g"].pop("last_played", None)
    else:
        window.config.profiles["g"]["last_played"] = previous
    previous_profile = dict(window.config.profiles["g"])
    window.game._failure_choice = lambda *args: "close"
    _running_launch(window)
    assert window.config.profiles["g"].get("last_played")
    if ending == "exit":
        # The controller can clear busy before delivering the exit code.
        window._on_busy("g", "launch", False)
        window._on_succeeded("g", "launch", 1)
    elif ending == "failure":
        window._on_failed("g", "launch", "Could not start")
    else:
        window._on_event("g", Event("error", "Could not start"))

    assert window.config.profiles["g"] == previous_profile
    assert json.loads(window.config.path.read_text())["profiles"]["g"] == previous_profile
    assert not window._snapshot("g")["status"].running
    assert window.game.badge.text() != "PLAYING"
    assert window.game.primary.text() != "PLAYING"
    assert window.game.step_label.text() != "Playing"
    assert window.game.kicker.isHidden()
    assert window.sidebar_games._rows["g"].playing_label.isHidden()
    assert window.library._cards["g"].property("running") is False
    assert "Playing" not in window.side_status.text()
    assert [tile.profile for tile in window.library.recent_tiles] == (["g"] if previous else [])
    # A queued watcher event cannot resurrect a launch whose failure is known.
    window._on_event("g", Event("game_running"))
    assert not window._snapshot("g")["status"].running


def test_success_keeps_play_history_and_external_games_still_record(window):
    _running_launch(window)
    played = window.config.profiles["g"]["last_played"]
    assert played
    window._on_busy("g", "launch", False)
    window._on_succeeded("g", "launch", 0)
    assert window.config.profiles["g"]["last_played"] == played
    window._on_result("g", "status", Status("installed"))
    window.config.profiles["g"].pop("last_played")
    window._on_result("g", "status", Status("running", running=True))
    assert window.config.profiles["g"]["last_played"]


def test_failed_launch_reprobe_reports_surviving_process_without_counting_play(window):
    _running_launch(window)
    window._on_failed("g", "launch", "Could not start")
    window._on_busy("g", "launch", False)
    window._on_result("g", "status", Status("running", running=True))
    assert window.game.kicker.text() == "NOW PLAYING"
    assert not window.game.kicker.isHidden()
    assert not window.config.profiles["g"].get("last_played")
    window._on_result("g", "status", Status("installed"))
    window._on_result("g", "status", Status("running", running=True))
    assert window.config.profiles["g"]["last_played"]


def test_retry_after_failure_records_a_new_successful_launch(window):
    _running_launch(window)
    window._on_failed("g", "launch", "Could not start")
    window._on_busy("g", "launch", False)
    # Retry before any status probe returns, so only a new launch clears the failure.
    window._start("g", "launch")
    window._on_busy("g", "launch", True)
    window._on_event("g", Event("game_running"))
    assert window.config.profiles["g"]["last_played"]
    window._on_succeeded("g", "launch", 0)
    assert window.config.profiles["g"]["last_played"]


def test_error_event_then_worker_failure_still_offers_retry(window):
    choices = []
    window.game._failure_choice = lambda title, message, retry: choices.append(retry) or "close"
    _running_launch(window)
    window._on_event("g", Event("error", "Could not start"))
    window._on_failed("g", "launch", "Could not start")
    assert choices == [True]


@pytest.mark.parametrize("evidence", ["window", "long"])
def test_a_launch_that_really_ran_keeps_its_play_history_after_a_non_zero_exit(
    window, evidence, monkeypatch
):
    """Wine games often exit non-zero after a real session: that play still counts."""
    import nadare.gui.main_window_parts.operations as ops

    window.config.profiles["g"]["last_played"] = "2026-01-01T00:00:00+00:00"
    window.game._failure_choice = lambda *args: "close"
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    _running_launch(window)
    played = window.config.profiles["g"]["last_played"]
    assert played != "2026-01-01T00:00:00+00:00"
    if evidence == "window":
        window._on_event("g", Event("window_seen"))
    else:
        clock[0] += ops.LAUNCH_EXIT_GRACE_SECONDS
    window._on_failed("g", "launch", "the game stopped responding")
    assert window.config.profiles["g"]["last_played"] == played


def test_a_quick_failure_without_a_window_still_restores_history(window, monkeypatch):
    import nadare.gui.main_window_parts.operations as ops

    window.config.profiles["g"]["last_played"] = "2026-01-01T00:00:00+00:00"
    window.game._failure_choice = lambda *args: "close"
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    _running_launch(window)
    clock[0] += ops.LAUNCH_EXIT_GRACE_SECONDS - 1
    window._on_busy("g", "launch", False)
    window._on_succeeded("g", "launch", 1)
    assert window.config.profiles["g"]["last_played"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("case", ["after_grace", "stopped_code"])
def test_a_normal_quit_with_a_non_zero_code_is_not_a_failed_launch(window, case, monkeypatch):
    """Same rule as the game page: after the grace, or a stop's signal code, it just closed."""
    import nadare.gui.main_window_parts.operations as ops
    from nadare.core.exitcode import STOPPED_EXIT_CODES

    window.config.profiles["g"]["last_played"] = "2026-01-01T00:00:00+00:00"
    window.game._failure_choice = lambda *args: "close"
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    _running_launch(window)
    played = window.config.profiles["g"]["last_played"]
    code = 1
    if case == "after_grace":
        clock[0] += ops.LAUNCH_EXIT_GRACE_SECONDS
    else:
        code = sorted(STOPPED_EXIT_CODES)[0]
    window._on_busy("g", "launch", False)
    window._on_succeeded("g", "launch", code)
    assert window._snapshot("g")["status"].state != "error"
    assert window.config.profiles["g"]["last_played"] == played
