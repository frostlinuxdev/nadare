# SPDX-License-Identifier: GPL-3.0-or-later
"""Remember startup failures across games, using the runner actually launched."""

import json
import time

import pytest

from nadare.core import paths
from nadare.core.events import Event


def record(game, runner="Old-Proton", outcome="failure", now=1000):
    from nadare.core.runner_failures import record_result

    return record_result(game, runner, outcome, now=now)


def test_two_games_notice_once_and_persist():
    assert not record("a")
    assert record("b")
    assert not record("c")
    state = json.loads((paths.data_dir() / "runner-failures.json").read_text())
    assert state["Old-Proton"]["noticed"] is True


def test_one_game_twice_is_not_enough():
    assert not record("a")
    assert not record("a")


def test_success_clears_failures_and_notice():
    record("a")
    assert record("b")
    assert not record("a", outcome="success")
    assert not record("c")
    assert record("d")


def test_window_expires():
    record("a", now=1000)
    assert not record("b", now=1000 + 86400)
    assert record("c", now=1001 + 86400)


@pytest.mark.parametrize("data", ["{", "[]", '{"x":null}', '{"x":{"games":[]}}', "\udcff"])
def test_corrupt_state_is_ignored(data):
    path = paths.data_dir() / "runner-failures.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(data.encode("utf-8", errors="surrogateescape"))
    assert not record("a")
    assert record("b")


def test_runner_change_rearms_notice_and_versions_are_separate():
    record("a")
    assert record("b")
    assert not record("a", runner="New-Proton", outcome="neutral")
    assert not record("b")
    assert record("c")
    assert not record("d", runner="New-Proton")


def test_state_is_bounded():
    for i in range(100):
        record(str(i), runner=f"Runner-{i}", now=1000 + i)
    state = json.loads((paths.data_dir() / "runner-failures.json").read_text())
    assert len(state) <= 32
    for i in range(150):
        record(str(i), now=2000 + i)
    state = json.loads((paths.data_dir() / "runner-failures.json").read_text())
    assert len(state["Old-Proton"]["games"]) <= 128


@pytest.fixture
def view():
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameView

    app = QApplication.instance() or QApplication([])
    page = GameView()
    page.shown = []
    page.opened = []

    def choice(title, message, can_retry, *, settings=False):
        page.shown.append((title, message, settings))
        return "settings" if settings else "close"

    page._failure_choice = choice
    page.settings_requested.connect(lambda: page.opened.append(True))
    yield page
    page.deleteLater()
    app.processEvents()


def launch(view, game, code=5, seconds=3, runner="Old-Proton", stop=False):
    view.set_profile(game, {"backend": "wine", "title": game})
    view.set_busy("launch")
    view.append_event(
        Event(
            "log",
            "runner",
            data={
                "runner": runner,
                "runner_default": "Existing-Pin",
                "runner_recommended": False,
            },
        )
    )
    view._last_step_at = time.time() - seconds
    if stop:
        view._busy_op = "stop"
    view.finish("launch", code)


def test_gui_notice_uses_launch_facts_and_settings(view, tmp_path, monkeypatch):
    launch(view, "a")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

    from nadare.gui.game_view_parts.console import ConsoleMixin

    monkeypatch.setattr(QMessageBox, "exec", lambda box: QDialog.exec(box))
    captured = []
    fake_choice = view._failure_choice

    def capture():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            captured.append(box.text())
            box.grab().save(str(tmp_path / "runner-notice.png"))
            button = next(b for b in box.buttons() if b.text() == "Game settings")
            button.click()

    def actual_choice(title, message, can_retry, *, settings=False):
        view.shown.append((title, message, settings))
        QTimer.singleShot(0, capture)
        return ConsoleMixin._failure_choice(view, title, message, can_retry, settings=settings)

    view._failure_choice = actual_choice
    launch(view, "b")
    view._failure_choice = fake_choice
    assert captured and "Old-Proton" in captured[0]
    assert (tmp_path / "runner-notice.png").is_file()
    title, message, settings = view.shown[-1]
    assert title == "This runner may be broken"
    assert "Old-Proton" in message and "Existing-Pin" in message
    assert "different games" in message.lower() and "Default · recommended" in message
    assert settings and len(view.opened) == 2
    launch(view, "c")
    assert sum(item[0] == title for item in view.shown) == 1


@pytest.mark.parametrize(
    "code,seconds,stop",
    [(0, 3, False), (1, 60, False), (-11, 60, False), (5, 3, True), (143, 3, False)],
)
def test_gui_does_not_count_normal_stops_or_late_crashes(view, code, seconds, stop):
    launch(view, "a", code, seconds, stop=stop)
    launch(view, "b")
    assert all(item[0] != "This runner may be broken" for item in view.shown)


def test_gui_success_clears_history(view):
    launch(view, "a")
    launch(view, "a", code=0, seconds=60)
    launch(view, "b")
    assert view.shown[-1][0] != "This runner may be broken"
    launch(view, "c", code=-11)
    assert view.shown[-1][0] == "This runner may be broken"


def test_notice_stays_acknowledged_after_window_until_runner_change():
    record("a")
    assert record("b")
    assert not record("c", now=1000 + 86400)
    assert not record("a", runner="New-Proton", outcome="neutral", now=1001 + 86400)
    assert not record("b", now=1002 + 86400)
    assert record("d", now=1003 + 86400)


def test_wine_crash_box_still_carries_the_runner_notice(view):
    """A startup Wine crash counts against the runner (#1733) and has its own box (#1734):
    the one-time notice must be in that box, not used up unseen."""
    crash = ("wine: Unhandled page fault on read access to 0000000000000000 at address "
             "0000000140001000 (thread 0024), starting debugger...")
    module = "PE\t0000000140000000-000000014000e000\tDeferred        game.exe"
    for game in ("a", "b"):
        view.set_profile(game, {"backend": "wine", "title": game})
        view.set_busy("launch")
        view.append_event(Event("log", "runner", data={
            "runner": "Old-Proton", "runner_default": "Existing-Pin", "runner_recommended": False,
        }))
        view.append_event(Event("log", crash))
        view.append_event(Event("log", module))
        view._last_step_at = time.time() - 3
        view.finish("launch", 5)
    first, second = view.shown
    assert "game.exe (Wine)" in first[1] and "may be broken" not in first[1]
    assert "game.exe (Wine)" in second[1]
    assert "Different games failed at startup on Old-Proton" in second[1]
    assert "Existing-Pin" in second[1]
