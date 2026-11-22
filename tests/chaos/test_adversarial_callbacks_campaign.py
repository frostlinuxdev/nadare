# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - fixtures imported below are requested by name
"""Delayed controller signals must not change a replacement game's page."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _out_of_order_helpers import make_window, no_background_work, qapp  # noqa: F401

from nadare.core.backends import Status
from nadare.core.events import Event


@pytest.mark.parametrize(
    "callback", ["event", "paused", "status", "success", "cancelled", "failed"]
)
def test_delayed_signal_cannot_change_readded_game(
    tmp_path, qapp, monkeypatch, no_background_work, callback
):
    from nadare.gui.workers import _WorkerBridge

    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "Old"}})
    try:
        window.open_game("g")
        window._worker_generations["g"] = window._profile_generation("g")
        old = SimpleNamespace(operation="status", payload=None, isRunning=lambda: False)
        window.controller._workers["g"] = old
        bridge = _WorkerBridge(window.controller, "g", "status", old, parent=window.controller)
        window._on_game_removed("g", SimpleNamespace(profile_kept=False, warnings=[]))
        config.add_profile("g", backend="wine", title="Replacement")
        config.save()
        window.refresh_library()
        window.open_game("g")
        window._busy["g"] = "install"
        window.controller._workers["g"] = SimpleNamespace(
            operation="install", payload=None, isRunning=lambda: False
        )
        observed = []
        for method in ["finish", "cancelled", "set_paused", "append_event", "set_status"]:
            monkeypatch.setattr(
                window.game,
                method,
                lambda *args, _method=method, **kwargs: observed.append(_method),
            )
        monkeypatch.setattr(window, "_start", lambda *a, **k: True)
        if callback == "event":
            bridge.forward_event(Event("progress", progress=0.9))
        elif callback == "paused":
            window.controller._pausing.add("g")
            bridge.on_cancelled()
        elif callback == "status":
            bridge.forward_result(Status(state="not_installed"))
        elif callback == "success":
            bridge.on_succeeded(0)
        elif callback == "cancelled":
            bridge.on_cancelled()
        else:
            bridge.on_failed("old failure")
        assert observed == [], f"stale {callback} changed replacement page: {observed}"
        assert window._busy.get("g") == "install"
    finally:
        window.controller._workers.clear()
        window._busy.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("callback", ["event", "status", "success", "cancelled"])
def test_late_signal_for_other_game_does_not_touch_current_page(
    tmp_path, qapp, monkeypatch, no_background_work, callback
):
    window, config = make_window(
        tmp_path, {"a": {"backend": "wine", "title": "A"}, "b": {"backend": "wine", "title": "B"}}
    )
    try:
        window.open_game("a")
        window._worker_generations["a"] = window._profile_generation("a")
        window.open_game("b")
        seen = []
        for method in ["finish", "cancelled", "append_event", "set_status"]:
            monkeypatch.setattr(
                window.game, method, lambda *a, _method=method, **k: seen.append(_method)
            )
        monkeypatch.setattr(window, "_start", lambda *a, **k: True)
        monkeypatch.setattr(window, "_maybe_offer_runner_cleanup", lambda: None)
        monkeypatch.setattr(window, "_maybe_offer_quality_cleanup", lambda *a: None)
        if callback == "event":
            window.controller.backend_event.emit("a", Event("progress", progress=0.75))
        elif callback == "status":
            window.controller.result.emit("a", "status", Status(state="not_installed"))
        elif callback == "success":
            window.controller.succeeded.emit("a", "install", 0)
        else:
            window.controller.cancelled.emit("a", "install")
        assert seen == []
        assert window._current_profile == "b"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("outcome", ["success", "cancelled", "failed"])
def test_superseded_worker_terminal_signal_keeps_current_install_busy(
    tmp_path, qapp, monkeypatch, no_background_work, outcome
):
    from nadare.gui.workers import _WorkerBridge

    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "Game"}})
    try:
        window.open_game("g")
        window._worker_generations["g"] = window._profile_generation("g")
        window._busy["g"] = "install"
        old = SimpleNamespace(operation="install", payload=None)
        current = SimpleNamespace(operation="install", payload=None, isRunning=lambda: True)
        window.controller._workers["g"] = current
        bridge = _WorkerBridge(window.controller, "g", "install", old, parent=window.controller)
        monkeypatch.setattr(window, "_start", lambda *a, **k: True)
        monkeypatch.setattr(window, "_maybe_offer_runner_cleanup", lambda: None)
        monkeypatch.setattr(window, "_maybe_offer_quality_cleanup", lambda *a: None)
        monkeypatch.setattr(window.game, "finish", lambda *a: None)
        monkeypatch.setattr(window.game, "fail", lambda *a, **k: None)
        monkeypatch.setattr(window.game, "cancelled", lambda *a: None)
        if outcome == "success":
            bridge.on_succeeded(0)
        elif outcome == "failed":
            bridge.on_failed("old failure")
        else:
            bridge.on_cancelled()
        assert window.controller._workers["g"] is current
        assert window._busy.get("g") == "install", "old worker cleared newer install in GUI"
    finally:
        window.controller._workers.clear()
        window._busy.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
