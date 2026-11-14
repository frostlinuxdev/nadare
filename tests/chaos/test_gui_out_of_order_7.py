# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""Removing a game must free its pending status probe, not keep its slot occupied."""

from __future__ import annotations

from types import SimpleNamespace

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)


class _RunningWorker:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.payload = None
        self.was_cancelled = False

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def cancel(self) -> None:
        self.was_cancelled = True

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return False


def test_removal_releases_a_pending_status_probe(
    tmp_path, qapp, no_background_work
):
    window, _config = make_window(tmp_path, {"g": {"backend": "wine", "title": "G"}})
    worker = _RunningWorker("status")
    window.controller._workers["g"] = worker
    try:
        assert window.controller.is_busy("g")
        window._on_game_removed("g", SimpleNamespace(profile_kept=False, warnings=[]))
        print("workers left:", window.controller._workers)
        print("still busy:", window.controller.is_busy("g"))
        print("worker was cancelled:", worker.was_cancelled)
        assert not window.controller.is_busy("g"), (
            "a removed game's status probe still owns its controller slot"
        )
    finally:
        window.controller._workers.clear()
        window.close()
