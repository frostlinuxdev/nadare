# SPDX-License-Identifier: GPL-3.0-or-later
"""Predownload on the game page (#1273): a button, its progress, and Play staying usable.

Predownloads run on their own controller, so the page's main button (LAUNCH) and the normal
operations are not held up by an hour-long download. Offscreen; no backend does real work.
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import process
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.events import Event


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _status(**data) -> Status:
    return Status(state="installed", version="3.3.0", data={"version": "3.3.0", **data})


@pytest.fixture
def view(qapp):
    from nadare.gui.game_view import GameView

    page = GameView()
    page.set_profile("zzz", {"backend": "direct-zzz", "title": "Zenless Zone Zero"})
    yield page
    page.deleteLater()
    qapp.processEvents()


# ---- the game page ---------------------------------------------------------------------------
def test_an_offered_predownload_shows_a_button(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    assert not view.predownload_btn.isHidden()
    assert "Predownload 3.4.0" in view.predownload_btn.text()
    assert view.predownload_btn.isEnabled()


def test_a_finished_predownload_says_so_and_cannot_be_clicked(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=True))
    assert not view.predownload_btn.isHidden()
    assert "3.4.0" in view.predownload_btn.text()
    assert "predownloaded" in view.predownload_btn.text().lower()
    assert not view.predownload_btn.isEnabled()


def test_no_offer_hides_the_button(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    view.set_status(_status())
    assert view.predownload_btn.isHidden()


def test_a_click_asks_for_a_predownload(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    asked, cancels = [], []
    view.predownload_requested.connect(lambda: asked.append(1))
    view.predownload_cancel_requested.connect(lambda: cancels.append(1))
    view.predownload_btn.click()
    assert asked == [1] and cancels == []


def test_while_running_it_shows_progress_and_a_click_cancels(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    asked, cancels = [], []
    view.predownload_requested.connect(lambda: asked.append(1))
    view.predownload_cancel_requested.connect(lambda: cancels.append(1))
    view.set_predownload_progress(True, 0.42)
    assert "42%" in view.predownload_btn.text()
    assert view.predownload_btn.isEnabled()
    view.predownload_btn.click()
    assert cancels == [1] and asked == []


def test_a_status_refresh_keeps_the_running_progress(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    view.set_predownload_progress(True, 0.5)
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    assert "50%" in view.predownload_btn.text()


def test_when_it_stops_the_button_follows_the_status_again(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    view.set_predownload_progress(True, 0.5)
    view.set_predownload_progress(False)
    assert "Predownload 3.4.0" in view.predownload_btn.text()
    assert "%" not in view.predownload_btn.text()


def test_play_stays_available_while_predownloading(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    view.set_predownload_progress(True, 0.1)
    assert view.primary.isEnabled()
    assert view.primary.text() != "CANCEL"


# ---- the worker ------------------------------------------------------------------------------
def test_the_worker_runs_a_predownload_holding_both_locks(tmp_path, monkeypatch):
    from nadare.gui import workers

    assert "predownload" in workers.OPERATIONS
    assert "predownload" not in workers.MUTATING  # quitting mid-way loses nothing: it resumes
    assert "predownload" not in workers.CAPPED
    config = Config.load(tmp_path / "config.json")
    config.add_profile("zzz", backend="direct-zzz")
    seen = []

    class FakeBackend:
        install_size_hint = None

        def predownload(self):
            seen.append(
                (
                    process.operation_in_progress("zzz"),
                    process.predownload_in_progress("zzz"),
                    process.launch_blocked("zzz"),
                )
            )

    monkeypatch.setattr(workers, "create", lambda *a, **k: FakeBackend())
    worker = workers.BackendWorker(config, "zzz", "predownload")
    done = []
    worker.succeeded.connect(lambda code: done.append(code))
    worker.run()
    assert seen == [(True, True, False)]
    assert done == [0]
    assert not process.operation_in_progress("zzz")
    assert not process.predownload_in_progress("zzz")


def test_the_worker_refuses_a_predownload_while_an_update_holds_the_game(tmp_path, monkeypatch):
    from nadare.gui import workers

    config = Config.load(tmp_path / "config.json")
    config.add_profile("zzz", backend="direct-zzz")
    ran = []

    class FakeBackend:
        install_size_hint = None

        def predownload(self):
            ran.append(1)

    monkeypatch.setattr(workers, "create", lambda *a, **k: FakeBackend())
    lock = process.OperationLock("zzz")
    lock.acquire()
    try:
        worker = workers.BackendWorker(config, "zzz", "predownload")
        failures = []
        worker.failed.connect(lambda message: failures.append(message))
        worker.run()
    finally:
        lock.release()
    assert ran == [] and failures
    assert not process.predownload_in_progress("zzz")


# ---- the window ------------------------------------------------------------------------------
@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("zzz", backend="direct-zzz", title="Zenless Zone Zero")
    win = mw.MainWindow(config, profile="zzz")
    errors = []
    # raising=False: before the feature exists the fixture must still close the window.
    monkeypatch.setattr(
        win, "_show_predownload_error", lambda p, m: errors.append((p, m)), raising=False
    )
    win._test_errors = errors
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _record(monkeypatch, controller, log, value=True):
    """Replace ``controller.start`` with one that only records ``(profile, operation)``."""

    def start(profile, operation, payload=None):
        log.append((profile, operation))
        return value

    monkeypatch.setattr(controller, "start", start)


def _wait(qapp, condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def test_the_window_has_its_own_predownload_controller(window):
    assert window.predownloads is not window.controller


def test_the_button_starts_a_predownload_on_its_own_controller(window, monkeypatch):
    started, main = [], []
    _record(monkeypatch, window.predownloads, started)
    _record(monkeypatch, window.controller, main)
    window.game.predownload_requested.emit()
    assert started == [("zzz", "predownload")]
    assert ("zzz", "predownload") not in main


def test_cancel_reaches_the_predownload_controller(window, monkeypatch):
    cancelled = []
    monkeypatch.setattr(window.predownloads, "cancel", lambda p: cancelled.append(p))
    window.game.predownload_cancel_requested.emit()
    assert cancelled == ["zzz"]


@pytest.mark.parametrize("operation", ["update", "repair", "install", "remake_prefix"])
def test_changes_to_the_game_wait_while_a_predownload_runs(window, monkeypatch, operation):
    main = []
    monkeypatch.setattr(window.predownloads, "is_busy", lambda p: p == "zzz")
    _record(monkeypatch, window.controller, main)
    monkeypatch.setattr(window, "_confirm_action", lambda p, op: True)
    window._on_game_action(operation)
    assert ("zzz", operation) not in main
    assert window._test_errors and window._test_errors[-1][0] == "zzz"


def test_launch_is_not_held_up_by_a_predownload(window, monkeypatch):
    main = []
    monkeypatch.setattr(window.predownloads, "is_busy", lambda p: p == "zzz")
    _record(monkeypatch, window.controller, main)
    monkeypatch.setattr(window, "_confirm_action", lambda p, op: True)
    window._on_game_action("launch")
    assert main == [("zzz", "launch")]
    assert window._test_errors == []


def test_progress_from_the_predownload_reaches_the_page(window, qapp):
    window.game.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    window.predownloads.busy.emit("zzz", "predownload", True)
    window.predownloads.backend_event.emit(
        "zzz",
        Event("progress", "download: 1 / 2", progress=0.5, data={"stage": "download"}),
    )
    qapp.processEvents()
    assert "50%" in window.game.predownload_btn.text()


def test_progress_of_another_game_does_not_touch_the_page(window, qapp):
    window.game.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    window.predownloads.backend_event.emit(
        "other", Event("progress", "x", progress=0.7, data={"stage": "download"})
    )
    qapp.processEvents()
    assert "70%" not in window.game.predownload_btn.text()


def test_success_refreshes_the_status(window, monkeypatch, qapp):
    asked = []
    _record(monkeypatch, window.controller, asked)
    window.game.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    window.predownloads.busy.emit("zzz", "predownload", True)
    window.predownloads.busy.emit("zzz", "predownload", False)
    window.predownloads.succeeded.emit("zzz", "predownload", 0)
    qapp.processEvents()
    assert ("zzz", "status") in asked
    assert "%" not in window.game.predownload_btn.text()


def test_a_failure_is_shown_and_the_button_comes_back(window, qapp):
    window.game.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    window.predownloads.busy.emit("zzz", "predownload", True)
    window.predownloads.busy.emit("zzz", "predownload", False)
    window.predownloads.failed.emit("zzz", "predownload", "not enough free space")
    qapp.processEvents()
    assert window._test_errors == [("zzz", "not enough free space")]
    assert "Predownload 3.4.0" in window.game.predownload_btn.text()


def test_a_cancel_brings_the_button_back_without_an_error(window, qapp):
    window.game.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    window.predownloads.busy.emit("zzz", "predownload", True)
    window.predownloads.busy.emit("zzz", "predownload", False)
    window.predownloads.cancelled.emit("zzz", "predownload")
    qapp.processEvents()
    assert window._test_errors == []
    assert "Predownload 3.4.0" in window.game.predownload_btn.text()


def test_closing_the_window_stops_the_predownload_controller(window, monkeypatch):
    stopped = []
    real = window.predownloads.shutdown
    monkeypatch.setattr(
        window.predownloads, "shutdown", lambda *a, **k: stopped.append(1) or real(*a, **k)
    )
    window.close()
    assert stopped


def test_switching_games_does_not_carry_a_running_predownload_over(view):
    view.set_status(_status(predownload_version="3.4.0", predownload_ready=False))
    view.set_predownload_progress(True, 0.3)
    view.set_profile("hsr", {"backend": "direct-hsr", "title": "Honkai: Star Rail"})
    view.set_status(_status())
    assert view.predownload_btn.isHidden()


def test_opening_a_game_shows_its_running_predownload(window, monkeypatch, qapp):
    monkeypatch.setattr(window.predownloads, "is_busy", lambda p: p == "zzz")
    window.open_game("zzz")
    qapp.processEvents()
    assert window.game.predownload_btn.text().startswith("Predownloading")
    assert not window.game.predownload_btn.isHidden()
