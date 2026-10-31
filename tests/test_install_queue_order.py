# SPDX-License-Identifier: GPL-3.0-or-later
"""One install at a time by default, and the waiting line can be reordered (no dragging)."""

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
    ctl = BackendController(config)
    ctl.spawned = []
    ctl.events = []

    def fake_spawn(profile, operation, payload=None):
        ctl._workers[profile] = FakeWorker(operation, payload)
        ctl.spawned.append((profile, operation))
        return True

    ctl._spawn = fake_spawn
    ctl.queued.connect(lambda p, o: ctl.events.append(("queued", p, o)))
    ctl.paused.connect(lambda p, o: ctl.events.append(("paused", p, o)))
    ctl.queue_changed.connect(lambda: ctl.events.append(("queue_changed",)))
    yield ctl
    from nadare.gui import workers as workers_mod

    workers_mod._detached.difference_update(
        {w for w in workers_mod._detached if isinstance(w, FakeWorker)}
    )


def finish(ctl, profile, how="succeeded"):
    worker = ctl._workers[profile]
    if how == "succeeded":
        ctl._on_succeeded(profile, worker.operation, 0, worker)
    else:
        ctl._on_cancelled(profile, worker.operation, worker)


def always(_profile: str) -> bool:
    return True


def test_the_default_is_one_install_at_a_time(controller):
    assert controller.max_active_installs() == 1
    controller.start("a", "install")
    controller.start("b", "install")
    assert controller.spawned == [("a", "install")]
    assert controller.is_waiting("b")


def test_the_waiting_line_is_listed_in_order(controller):
    for name in ("a", "b", "c", "d"):
        controller.start(name, "install")
    assert controller.queue_order() == ["b", "c", "d"]


def test_move_to_front_puts_a_waiting_install_first(controller):
    for name in ("a", "b", "c", "d"):
        controller.start(name, "install")
    controller.events.clear()
    assert controller.move_to_front("d")
    assert controller.queue_order() == ["d", "b", "c"]
    assert ("queue_changed",) in controller.events
    finish(controller, "a")
    assert controller.spawned[-1] == ("d", "install")


def test_move_to_front_refuses_what_is_not_waiting_or_already_first(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    assert not controller.move_to_front("a")  # running
    assert not controller.move_to_front("d")  # unknown / idle
    assert not controller.move_to_front("b")  # already first
    assert controller.queue_order() == ["b", "c"]


def test_start_now_pauses_the_running_install_and_runs_this_one_next(controller):
    controller.start("a", "install", payload="PA")
    controller.start("b", "install")
    controller.start("c", "install")
    assert controller.start_now("c", always)
    assert controller._workers["a"].cancelled  # pause was asked for
    assert controller.queue_order() == ["c", "b"]
    assert ("c", "install") not in controller.spawned  # not before the pause lands
    finish(controller, "a", "cancelled")
    assert controller.spawned[-1] == ("c", "install")
    # The paused install waits at the front of the line, not paused, not lost.
    assert controller.queue_order() == ["a", "b"]
    assert controller.is_waiting("a") and not controller.is_paused("a")
    assert controller.max_active_installs() == 1
    assert sum(1 for w in controller._workers.values() if w.isRunning()) == 1
    # Its payload survives so it resumes with the same arguments.
    assert controller._waiting["a"] == ("install", "PA")
    finish(controller, "c")
    assert controller.spawned[-1] == ("a", "install")


def test_start_now_does_not_announce_a_pause_to_the_page(controller):
    controller.start("a", "install")
    controller.start("b", "install")
    controller.start_now("b", always)
    controller.events.clear()
    finish(controller, "a", "cancelled")
    assert ("paused", "a", "install") not in controller.events
    assert ("queued", "a", "install") in controller.events


def test_start_now_needs_a_running_install_that_can_pause(controller):
    controller.start("a", "install")
    controller.start("b", "install")
    assert not controller.start_now("b", lambda _p: False)
    assert not controller._workers["a"].cancelled
    assert controller.queue_order() == ["b"]


def test_start_now_refuses_a_profile_that_is_not_waiting(controller):
    controller.start("a", "install")
    assert not controller.start_now("a", always)
    assert not controller.start_now("d", always)


def test_can_start_now_matches_start_now(controller):
    controller.start("a", "install")
    controller.start("b", "install")
    assert controller.can_start_now("b", always)
    assert not controller.can_start_now("b", lambda _p: False)
    assert not controller.can_start_now("a", always)


def test_start_now_only_pauses_installs_not_launches(controller):
    controller.start("a", "launch")
    controller.start("b", "install")
    assert controller.spawned == [("a", "launch"), ("b", "install")]
    controller.start("c", "install")
    assert not controller.can_start_now("c", lambda p: p == "a")


def test_a_cancel_during_start_now_does_not_bring_the_install_back(controller):
    controller.start("a", "install")
    controller.start("b", "install")
    controller.start_now("b", always)
    controller.cancel("a")
    finish(controller, "a", "cancelled")
    assert controller.spawned[-1] == ("b", "install")
    assert "a" not in controller.queue_order()
    assert not controller.is_waiting("a")


def test_an_install_that_finishes_before_the_pause_lands_is_not_requeued(controller):
    controller.start("a", "install")
    controller.start("b", "install")
    controller.start_now("b", always)
    finish(controller, "a", "succeeded")
    assert controller.spawned[-1] == ("b", "install")
    assert "a" not in controller.queue_order()
    finish(controller, "b")
    assert controller.spawned[-1] == ("b", "install")  # nothing stale restarts a


def test_removing_the_waiting_game_drops_it_from_the_line(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.events.clear()
    controller.forget("b")
    assert controller.queue_order() == ["c"]
    assert ("queue_changed",) in controller.events


def test_the_line_announces_every_change(controller):
    controller.start("a", "install")
    controller.events.clear()
    controller.start("b", "install")
    assert ("queue_changed",) in controller.events
    controller.events.clear()
    controller.cancel("b")
    assert ("queue_changed",) in controller.events
    controller.start("b", "install")
    controller.events.clear()
    finish(controller, "a")
    assert ("queue_changed",) in controller.events


def test_an_install_already_pausing_is_not_asked_twice(controller):
    controller._config.data["max_concurrent_installs"] = 2
    for name in ("a", "b", "c", "d"):
        controller.start(name, "install")
    assert controller.start_now("c", always)  # pauses b, the newest
    assert controller._workers["b"].cancelled
    assert controller.start_now("d", always)  # must pick a, not b again
    assert controller._workers["a"].cancelled
    assert controller.queue_order()[:2] == ["d", "c"]


def test_the_overview_lists_running_waiting_and_paused_installs(controller):
    controller._config.data["max_concurrent_installs"] = 1
    controller.start("a", "install")
    controller.start("b", "update")
    controller.start("c", "repair")
    controller.start("d", "install")
    controller.pause("d")  # waiting -> paused
    overview = controller.install_overview()
    assert overview.running == [("a", "install")]
    assert overview.waiting == [("b", "update"), ("c", "repair")]
    assert overview.paused == [("d", "install")]


def test_the_overview_ignores_launches_and_background_work(controller):
    controller.start("a", "launch")
    controller.start("b", "status")
    overview = controller.install_overview()
    assert overview.running == [] and overview.waiting == [] and overview.paused == []


def test_the_overview_follows_the_line_when_it_is_reordered(controller):
    for name in ("a", "b", "c"):
        controller.start(name, "install")
    controller.move_to_front("c")
    assert [name for name, _op in controller.install_overview().waiting] == ["c", "b"]


def test_a_paused_launch_never_appears(controller):
    controller._paused["a"] = ("launch", None)
    assert controller.install_overview().paused == []
