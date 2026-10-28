# SPDX-License-Identifier: GPL-3.0-or-later
"""Pause and resume for downloads (issue #538): the controller layer."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.workers import PAUSABLE, BackendController


class FakeWorker:
    def __init__(self, operation: str, payload: object = None) -> None:
        self.operation = operation
        self.payload = payload
        self.cancelled = False
        self._running = True

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def cancel(self) -> None:
        self.cancelled = True

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return not self._running


@pytest.fixture
def controller(qtbot, tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    ctl = BackendController(config)
    ctl.spawned = []
    ctl._spawn = lambda profile, operation, payload=None: ctl.spawned.append(
        (profile, operation, payload)
    )
    ctl.events = []
    ctl.paused.connect(lambda p, o: ctl.events.append(("paused", p, o)))
    ctl.cancelled.connect(lambda p, o: ctl.events.append(("cancelled", p, o)))
    ctl.succeeded.connect(lambda p, o, c: ctl.events.append(("succeeded", p, o)))
    ctl.failed.connect(lambda p, o, m: ctl.events.append(("failed", p, o)))
    return ctl


def running(controller, operation="install", payload="PAYLOAD"):
    worker = FakeWorker(operation, payload)
    controller._workers["p"] = worker
    return worker


def test_install_update_and_repair_are_pausable():
    assert {"install", "update", "repair"} <= set(PAUSABLE)
    assert not ({"launch", "status", "verify", "uninstall", "login"} & set(PAUSABLE))


@pytest.mark.parametrize("operation", ["install", "update", "repair"])
def test_pause_cancels_a_pausable_operation(controller, operation):
    worker = running(controller, operation)
    assert controller.pause("p") is True
    assert worker.cancelled is True


@pytest.mark.parametrize("operation", ["launch", "status", "verify", "uninstall", "login"])
def test_pause_refuses_everything_else(controller, operation):
    worker = running(controller, operation)
    assert controller.pause("p") is False
    assert worker.cancelled is False


def test_pause_with_nothing_running_is_refused(controller):
    assert controller.pause("p") is False


def test_a_second_pause_while_pausing_is_refused(controller):
    running(controller)
    assert controller.pause("p") is True
    assert controller.pause("p") is False


def test_the_cancel_that_follows_a_pause_is_reported_as_paused(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.events == [("paused", "p", "install")]
    assert controller.is_paused("p") is True
    assert controller.paused_operation("p") == "install"
    assert controller.is_busy("p") is False


def test_a_plain_cancel_is_still_a_cancel(controller):
    worker = running(controller)
    controller.cancel("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.events == [("cancelled", "p", "install")]
    assert controller.is_paused("p") is False


def test_finishing_before_the_pause_lands_is_not_a_pause(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_succeeded("p", "install", 0, worker)
    assert controller.events == [("succeeded", "p", "install")]
    assert controller.is_paused("p") is False


def test_failing_while_pausing_is_not_a_pause(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_failed("p", "install", "boom", worker)
    assert controller.events == [("failed", "p", "install")]
    assert controller.is_paused("p") is False
    assert controller.pause("p") is False


def test_resume_runs_the_same_operation_with_the_same_payload(controller):
    worker = running(controller, "update", {"x": 1})
    controller.pause("p")
    controller._on_cancelled("p", "update", worker)
    assert controller.resume("p") is True
    assert controller.spawned == [("p", "update", {"x": 1})]
    assert controller.is_paused("p") is False


def test_resume_without_a_pause_is_refused(controller):
    assert controller.resume("p") is False
    assert controller.spawned == []


def test_resume_twice_runs_once(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.resume("p") is True
    controller._workers["p"] = FakeWorker("install")
    assert controller.resume("p") is False
    assert len(controller.spawned) == 1


def test_cancel_while_paused_drops_the_pause_and_reports_a_cancel(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    controller.events.clear()
    controller.cancel("p")
    assert controller.is_paused("p") is False
    assert controller.events == [("cancelled", "p", "install")]


def test_starting_another_operation_drops_the_pause(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.start("p", "uninstall") is True
    assert controller.is_paused("p") is False
    assert controller.spawned == [("p", "uninstall", None)]


def test_paused_state_is_per_profile(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.is_paused("other") is False
    assert controller.paused_operation("other") == ""


def test_shutdown_forgets_paused_operations(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    controller.shutdown(wait_ms=0)
    assert controller.is_paused("p") is False


def test_a_refused_resume_keeps_the_pause(controller):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    controller._workers["p"] = FakeWorker("uninstall")
    assert controller.resume("p") is False
    assert controller.is_paused("p") is True
    assert controller.paused_operation("p") == "install"


@pytest.mark.parametrize("operation", ["status", "news", "media", "check_update"])
def test_a_background_refresh_does_not_forget_the_pause(controller, operation):
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.start("p", operation) is True
    assert controller.is_paused("p") is True
    assert controller.paused_operation("p") == "install"


def test_cancel_while_the_pause_is_landing_is_a_cancel(controller):
    worker = running(controller)
    controller.pause("p")
    controller.cancel("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.events == [("cancelled", "p", "install")]
    assert controller.is_paused("p") is False


def test_a_cancel_after_the_worker_already_finished_still_beats_a_landing_pause(controller):
    """The thread can exit before the queued 'cancelled' slot runs; Cancel must still win."""
    worker = running(controller)
    controller.pause("p")
    worker._running = False  # finished, its cancelled signal not delivered yet
    controller.cancel("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.events == [("cancelled", "p", "install")]
    assert controller.is_paused("p") is False


def test_forget_drops_a_paused_operation(controller):
    """Removing a game must not leave a paused install that a re-added game would resume."""
    worker = running(controller)
    controller.pause("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.is_paused("p") is True
    controller.forget("p")
    assert controller.is_paused("p") is False
    assert controller.resume("p") is False
    assert controller.spawned == []


def test_forget_also_clears_a_pause_that_has_not_landed(controller):
    worker = running(controller)
    controller.pause("p")
    controller.forget("p")
    controller._on_cancelled("p", "install", worker)
    assert controller.events == [("cancelled", "p", "install")]
    assert controller.is_paused("p") is False


def test_pausing_an_install_that_is_queued_behind_a_status_probe_works(controller):
    """INSTALL clicked while a status probe runs is only queued; the visible Pause must act."""
    probe = running(controller, "status")
    assert controller.start("p", "install", "PAYLOAD") is True  # queued behind the probe
    assert probe.cancelled is True
    assert controller.pause("p") is True
    assert ("paused", "p", "install") in controller.events
    assert controller.is_paused("p") is True
    assert controller.is_busy("p") is False
    assert controller.resume("p") is True
    assert controller.spawned == [("p", "install", "PAYLOAD")]


def test_pausing_a_queued_operation_that_cannot_be_paused_is_refused(controller):
    running(controller, "status")
    assert controller.start("p", "launch") is True
    assert controller.pause("p") is False
