# SPDX-License-Identifier: GPL-3.0-or-later
"""Background refreshes are preemptible; user operations still serialise."""

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
    """Just enough of ``BackendWorker`` for the controller's bookkeeping."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.cancelled = False
        self._running = True

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def cancel(self) -> None:
        self.cancelled = True

    def finish(self) -> None:
        self._running = False

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return not self._running


@pytest.fixture
def controller(qtbot, tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    return BackendController(config)


def test_background_is_preempted_by_user_operation(controller):
    status = FakeWorker("status")
    controller._workers["p"] = status
    spawned: list[tuple[str, object]] = []

    def fake_spawn(profile, operation, payload=None):
        spawned.append((operation, payload))

    controller._spawn = fake_spawn
    busy: list[tuple[str, bool]] = []
    controller.busy.connect(lambda _p, op, active: busy.append((op, active)))

    assert controller.start("p", "launch") is True
    assert status.cancelled is True
    assert controller._pending["p"] == ("launch", None)
    # The page is told the user operation is under way immediately.
    assert ("launch", True) in busy

    status.finish()
    controller._finish("p", "status", status)
    controller._on_worker_finished("p", status)

    assert spawned == [("launch", None)]
    assert "p" not in controller._pending


def test_background_refresh_is_skipped_while_busy(controller):
    controller._workers["p"] = FakeWorker("launch")
    assert controller.start("p", "status") is False
    assert "p" not in controller._pending


def test_second_user_operation_is_rejected_while_user_busy(controller):
    controller._workers["p"] = FakeWorker("launch")
    assert controller.start("p", "update") is False
    assert "p" not in controller._pending


def test_second_user_operation_rejected_while_one_is_queued(controller):
    controller._workers["p"] = FakeWorker("status")
    assert controller.start("p", "launch") is True
    assert controller.start("p", "update") is False
    assert controller._pending["p"] == ("launch", None)


def test_cancel_drops_a_queued_user_operation(controller):
    status = FakeWorker("status")
    controller._workers["p"] = status
    spawned: list[str] = []
    controller._spawn = lambda profile, operation, payload=None: spawned.append(operation)
    cancelled: list[str] = []
    busy: list[tuple[str, bool]] = []
    controller.cancelled.connect(lambda _p, op: cancelled.append(op))
    controller.busy.connect(lambda _p, op, active: busy.append((op, active)))

    assert controller.start("p", "launch") is True
    controller.cancel("p")
    assert cancelled == ["launch"]
    assert ("launch", False) in busy
    assert not controller.is_busy("p") or status.isRunning()

    status.finish()
    controller._finish("p", "status", status)
    controller._on_worker_finished("p", status)
    assert spawned == [], "a cancelled queued operation must never start"


def test_shutdown_never_starts_a_queued_operation(controller):
    status = FakeWorker("status")
    controller._workers["p"] = status
    spawned: list[str] = []
    controller._spawn = lambda profile, operation, payload=None: spawned.append(operation)

    from nadare.gui import workers

    assert controller.start("p", "launch") is True
    controller.shutdown(wait_ms=0)
    status.finish()
    workers._detached.discard(status)
    controller._on_worker_finished("p", status)
    assert spawned == []


def test_shutdown_detaches_a_worker_that_outlives_the_wait(controller):
    """Closing during a slow fetch must not destroy a running QThread (Qt aborts)."""
    import time

    from PyQt6.QtCore import QThread

    from nadare.gui import workers

    class Slow(QThread):
        operation = "news"

        def run(self) -> None:
            time.sleep(0.5)

        def cancel(self) -> None:
            pass

    slow = Slow(controller)
    controller._workers["p"] = slow
    slow.start()
    controller.shutdown(wait_ms=10)
    assert slow.isRunning()
    assert slow.parent() is None, "a still-running worker must outlive its window"
    assert slow in workers._detached
    assert workers.wait_detached(3000) is True
    assert not workers._detached


def test_shutdown_waits_within_one_overall_deadline(controller, monkeypatch):
    """Regression (06-8): N stuck workers must not each burn the full wait_ms."""
    from nadare.gui import workers

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr(workers, "time", clock)

    class Blocking:
        operation = "news"

        def __init__(self, clock: Clock) -> None:
            self._clock = clock
            self.requested: list[int] = []

        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return True

        def cancel(self) -> None:
            pass

        def wait(self, ms: int = 0) -> bool:
            self.requested.append(ms)
            self._clock.now += ms / 1000.0
            return False

        def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
            pass

    first = Blocking(clock)
    second = Blocking(clock)
    controller._workers["a"] = first
    controller._workers["b"] = second

    controller.shutdown(wait_ms=1000)

    assert first.requested == [1000]
    assert second.requested == [0], "the second worker must share the same deadline"
    workers._detached.discard(first)
    workers._detached.discard(second)


def test_mutating_profiles_lists_only_running_mutations(controller):
    from nadare.gui import workers

    class W:
        def __init__(self, operation: str, running: bool) -> None:
            self.operation = operation
            self._running = running

        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return self._running

    controller._workers["installing"] = W("install", True)
    controller._workers["probing"] = W("status", True)
    controller._workers["idle"] = W("update", False)
    assert controller.mutating_profiles() == ["installing"]
    assert workers.is_mutating("install") is True
    assert workers.is_mutating("status") is False

