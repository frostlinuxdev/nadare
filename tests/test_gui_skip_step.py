# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page offers "Skip this step" while the VC++ installer runs, and only then."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.core.events import Cancel, Event
from nadare.gui import game_view
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


SKIPPABLE = Event("step", "Installing that Windows component…", data={"skippable": True})


def _view(qapp):
    return game_view.GameView()


def test_the_skip_button_is_hidden_until_a_skippable_step(qapp):
    view = _view(qapp)
    assert view.skip_button.isHidden()
    view.append_event(Event("step", "Downloading a Windows component…"))
    assert view.skip_button.isHidden()
    view.append_event(SKIPPABLE)
    assert not view.skip_button.isHidden()


def test_log_lines_do_not_hide_the_button_while_the_installer_runs(qapp):
    view = _view(qapp)
    view.append_event(SKIPPABLE)
    view.append_event(Event("log", "Proton: starting vc_redist.x64.exe"))
    view.append_event(Event("progress", "", progress=None, data={"indeterminate": True}))
    assert not view.skip_button.isHidden()


@pytest.mark.parametrize(
    "event",
    [
        Event("step", "Starting the game…"),
        Event("status", "Skipped the Windows component"),
        Event("done", "done"),
        Event("error", "boom"),
    ],
)
def test_the_next_step_status_or_end_hides_the_button(qapp, event):
    view = _view(qapp)
    view.append_event(SKIPPABLE)
    view.append_event(event)
    assert view.skip_button.isHidden()


def test_cancel_hides_the_button(qapp):
    view = _view(qapp)
    view.append_event(SKIPPABLE)
    view.cancelled()
    assert view.skip_button.isHidden()


def test_clicking_it_asks_for_a_skip_and_hides_it(qapp):
    view = _view(qapp)
    asked: list[int] = []
    view.skip_step_requested.connect(lambda: asked.append(1))
    view.append_event(SKIPPABLE)
    view.skip_button.click()
    assert asked == [1]
    assert view.skip_button.isHidden(), "a second click must not skip the next thing"


def test_the_button_text_says_it_only_skips_this_step(qapp):
    view = _view(qapp)
    assert "skip" in view.skip_button.text().lower()
    assert "launch" in view.skip_button.toolTip().lower()


# ---- the window and the worker manager pass it on -------------------------------------------
def test_the_window_forwards_a_skip_for_the_current_profile(qapp, tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True  # no real workers
    skipped: list[str] = []
    window.controller.skip_step = lambda profile: skipped.append(profile)
    window._current_profile = "czn"
    window.game.skip_step_requested.emit()
    assert skipped == ["czn"]
    window.close()


class _Worker:
    def __init__(self, running=True):
        self.operation = "launch"
        self._running = running
        self.skipped = 0

    def isRunning(self):  # noqa: N802 - Qt naming
        return self._running

    def skip_step(self):
        self.skipped += 1


@pytest.fixture
def controller(qtbot, tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    return BackendController(config)


def test_the_controller_skips_only_a_running_worker(controller):
    live = _Worker()
    controller._workers["p"] = live
    controller.skip_step("p")
    assert live.skipped == 1
    done = _Worker(running=False)
    controller._workers["p"] = done
    controller.skip_step("p")
    assert done.skipped == 0
    controller.skip_step("unknown")  # no worker: nothing happens, no crash


def test_the_worker_sets_the_skip_flag_on_its_token(qtbot, tmp_path):
    from nadare.gui.workers import BackendWorker

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    worker = BackendWorker(config, "p", "launch", None)
    assert isinstance(worker._cancel_token, Cancel)
    worker.skip_step()
    assert worker._cancel_token.skip_requested is True
    assert worker._cancel_token.cancelled is False
