# SPDX-License-Identifier: GPL-3.0-or-later
"""#770: only a few installs, updates and repairs run at once; the rest wait their turn."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.workers import BackendController


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
    for name in ("a", "b", "c", "d"):
        config.add_profile(name, backend="wine", title=name.upper())
    config.data["max_concurrent_installs"] = 2  # these tests are about the cap mechanism
    ctl = BackendController(config)
    ctl.spawned = []
    ctl.events = []

    def fake_spawn(profile, operation, payload=None):
        worker = FakeWorker(operation, payload)
        ctl._workers[profile] = worker
        ctl.spawned.append((profile, operation))
        return True

    ctl._spawn = fake_spawn
    ctl.queued.connect(lambda p, o: ctl.events.append(("queued", p, o)))
    ctl.busy.connect(lambda p, o, on: ctl.events.append(("busy", p, o, on)))
    ctl.cancelled.connect(lambda p, o: ctl.events.append(("cancelled", p, o)))
    ctl.paused.connect(lambda p, o: ctl.events.append(("paused", p, o)))
    yield ctl
    # shutdown() parks workers that are still "running" in a process-wide set; ours are fakes.
    from nadare.gui import workers as workers_mod

    workers_mod._detached.difference_update(
        {w for w in workers_mod._detached if isinstance(w, FakeWorker)}
    )


def finish(ctl, profile, how="succeeded"):
    worker = ctl._workers[profile]
    if how == "succeeded":
        ctl._on_succeeded(profile, worker.operation, 0, worker)
    elif how == "failed":
        ctl._on_failed(profile, worker.operation, "boom", worker)
    else:
        ctl._on_cancelled(profile, worker.operation, worker)


def test_two_installs_run_and_the_third_waits(controller):
    assert controller.start("a", "install") and controller.start("b", "install")
    assert controller.start("c", "install")  # accepted, but it has to wait
    assert controller.spawned == [("a", "install"), ("b", "install")]
    assert ("queued", "c", "install") in controller.events
    assert controller.is_busy("c")
    assert controller.is_waiting("c") and not controller.is_waiting("a")


def test_a_finished_install_hands_its_slot_to_the_first_waiting_one(controller):
    for name in ("a", "b", "c", "d"):
        controller.start(name, "install")
    assert controller.spawned == [("a", "install"), ("b", "install")]
    finish(controller, "a")
    assert controller.spawned[-1] == ("c", "install")  # first come, first served
    assert controller.is_waiting("d")
    finish(controller, "b")
    assert controller.spawned[-1] == ("d", "install")
    assert not controller.is_waiting("c") and not controller.is_waiting("d")


@pytest.mark.parametrize("how", ["failed", "cancelled"])
def test_a_failed_or_cancelled_install_also_frees_its_slot(controller, how):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    finish(controller, "a", how)
    assert controller.spawned[-1] == ("c", "install")


@pytest.mark.parametrize("operation", ["update", "repair"])
def test_updates_and_repairs_count_toward_the_cap(controller, operation):
    controller.start("a", "install")
    controller.start("b", operation)
    controller.start("c", "install")
    assert controller.is_waiting("c")


@pytest.mark.parametrize("operation", ["launch", "status", "verify", "uninstall", "news", "stop"])
def test_other_operations_are_never_held_back(controller, operation):
    controller.start("a", "install")
    controller.start("b", "install")
    assert controller.start("c", operation)
    assert controller.spawned[-1] == ("c", operation)
    assert not controller.is_waiting("c")


def test_a_waiting_install_can_be_cancelled_and_never_starts(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.events.clear()
    controller.cancel("c")
    assert not controller.is_waiting("c") and not controller.is_busy("c")
    assert ("cancelled", "c", "install") in controller.events
    assert ("busy", "c", "install", False) in controller.events
    finish(controller, "a")
    assert ("c", "install") not in controller.spawned[2:]


def test_pausing_a_waiting_install_keeps_it_for_resume(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install", payload="P")
    assert controller.pause("c")
    assert controller.is_paused("c") and not controller.is_waiting("c")
    assert ("paused", "c", "install") in controller.events
    finish(controller, "a")
    assert ("c", "install") not in controller.spawned  # paused, so it does not take the slot
    assert controller.resume("c")
    assert controller.spawned[-1] == ("c", "install")


def test_a_second_request_for_a_waiting_profile_is_refused(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    assert controller.start("c", "install") is False
    assert controller.start("c", "update") is False


def test_the_cap_is_a_setting_and_is_kept_sane(controller):
    controller._config.data["max_concurrent_installs"] = 1
    controller.start("a", "install")
    controller.start("b", "install")
    assert controller.is_waiting("b")
    controller._config.data["max_concurrent_installs"] = 0  # nonsense stays at one
    assert controller.max_active_installs() == 1
    controller._config.data["max_concurrent_installs"] = 99
    assert controller.max_active_installs() == 4
    controller._config.data["max_concurrent_installs"] = "many"
    assert controller.max_active_installs() == 1
    del controller._config.data["max_concurrent_installs"]
    assert controller.max_active_installs() == 1


def test_removing_a_game_drops_its_place_in_the_queue(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.forget("c")
    assert not controller.is_waiting("c")
    finish(controller, "a")
    assert ("c", "install") not in controller.spawned


def test_shutdown_cancels_everything_that_is_waiting(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.events.clear()
    controller.shutdown(wait_ms=0)
    assert not controller.is_waiting("c")
    assert ("cancelled", "c", "install") in controller.events


def test_a_waiting_install_released_after_a_probe_still_respects_the_cap(controller):
    """A user install queued behind a status probe goes through the same gate."""
    controller.start("a", "install")
    controller.start("b", "install")
    probe = FakeWorker("status")
    controller._workers["c"] = probe
    controller.start("c", "install")  # waits for the probe first
    probe._running = False
    controller._on_worker_finished("c", probe)
    assert controller.is_waiting("c")
    assert ("c", "install") not in controller.spawned


def test_the_fake_workers_of_the_shutdown_test_do_not_stay_detached(controller):
    from nadare.gui import workers as workers_mod

    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.shutdown(wait_ms=0)
    assert any(isinstance(w, FakeWorker) for w in workers_mod._detached)  # parked, as in the app
    # (the fixture's teardown removes them again; test_the_detached_set_is_clean checks that)


def test_the_detached_set_is_clean_between_tests():
    from nadare.gui import workers as workers_mod

    assert not [w for w in workers_mod._detached if isinstance(w, FakeWorker)]
