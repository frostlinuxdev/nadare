# SPDX-License-Identifier: GPL-3.0-or-later
"""A worker that finishes late must not clear the busy state of the operation that replaced it."""

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
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.payload = None

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return True


@pytest.fixture
def controller(qtbot, tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    c = BackendController(config)
    c.busy_events = []
    c.busy.connect(lambda profile, op, active: c.busy_events.append((profile, op, active)))
    return c


def test_the_current_worker_finishing_clears_busy(controller):
    worker = FakeWorker("install")
    controller._workers["p"] = worker
    controller._finish("p", "install", worker)
    assert controller.busy_events == [("p", "install", False)]
    assert "p" not in controller._workers


def test_a_stale_worker_does_not_clear_a_newer_operation(controller):
    stale, newer = FakeWorker("install"), FakeWorker("install")
    controller._workers["p"] = newer
    controller._finish("p", "install", stale)
    assert controller.busy_events == []
    assert controller._workers["p"] is newer


@pytest.mark.parametrize("path", ["succeeded", "failed", "cancelled"])
def test_every_finish_path_ignores_a_stale_worker(controller, path):
    stale, newer = FakeWorker("install"), FakeWorker("install")
    controller._workers["p"] = newer
    if path == "succeeded":
        controller._on_succeeded("p", "install", 0, stale)
    elif path == "failed":
        controller._on_failed("p", "install", "boom", stale)
    else:
        controller._on_cancelled("p", "install", stale)
    assert [e for e in controller.busy_events if e[2] is False] == []
    assert controller._workers["p"] is newer


def test_the_outcome_signal_still_reaches_the_ui_for_a_stale_worker(controller):
    got = []
    controller.succeeded.connect(lambda p, o, c: got.append((p, o, c)))
    controller._workers["p"] = FakeWorker("install")
    controller._on_succeeded("p", "install", 0, FakeWorker("install"))
    assert got == [("p", "install", 0)]
