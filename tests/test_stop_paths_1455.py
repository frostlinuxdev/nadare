# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-2/R4-3: every Wine game stops the same way.

* Stop ends the game's processes, then the prefix's wineserver (CZN never did, so Wine helpers
  could stay alive in its prefix).
* A game started by another Nadare window (Flatpak: separate process namespaces) is asked to
  stop through the run lock, by one shared helper, for every Wine backend (generic Wine had none).
* A game holds the run lock while it runs, and the lock's stop request ends the game's processes,
  not only Nadare's wait (CZN only cancelled; generic Wine never held the lock).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import wine as wine_mod
from nadare.backends.czn import backend_parts
from nadare.backends.czn.backend import CznBackend
from nadare.core import runlock


# ------------------------------------------------------------ shared helper
def test_stop_elsewhere_asks_and_waits(monkeypatch, tmp_path):
    asked, states = [], iter([True, True, False])
    monkeypatch.setattr(runlock, "request_stop", lambda p: asked.append(Path(p)))
    monkeypatch.setattr(runlock, "running", lambda p: next(states, False))
    said = []
    wine_mod.stop_elsewhere(tmp_path, said.append, timeout=5, poll=0)
    assert asked == [tmp_path]
    assert not any("did not stop" in s for s in said)


def test_stop_elsewhere_says_when_the_game_did_not_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(runlock, "request_stop", lambda p: None)
    monkeypatch.setattr(runlock, "running", lambda p: True)
    said = []
    wine_mod.stop_elsewhere(tmp_path, said.append, timeout=0, poll=0)
    assert any("did not stop" in s for s in said)


# ----------------------------------------------------------------------- CZN
def _czn(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"runner": "dwproton"}))
    launching = backend_parts.launching
    killed = []
    monkeypatch.setattr(launching.os, "kill", lambda pid, sig: killed.append(pid))
    servers = []
    monkeypatch.setattr(
        launching, "stop_runner_wineserver", lambda **kw: servers.append(kw), raising=False
    )
    monkeypatch.setattr(backend, "_runner_for_stop", lambda: "/runners/dwproton")
    return backend, killed, servers


def test_czn_stop_also_stops_the_prefix_wineserver(tmp_path, monkeypatch):
    backend, killed, servers = _czn(tmp_path, monkeypatch)
    pids = iter([[101, 102], [], []])
    monkeypatch.setattr(backend, "_game_pids", lambda helpers=True: next(pids, []))
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: False)
    backend.stop()
    assert killed[:2] == [101, 102]
    assert servers and servers[0]["prefix"] == backend.prefix()
    assert servers[0]["proton"] == "/runners/dwproton"


def test_czn_stop_with_no_game_left_still_stops_lingering_helpers(tmp_path, monkeypatch):
    backend, _killed, servers = _czn(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_game_pids", lambda helpers=True: [])
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: False)
    backend.stop()
    assert servers, "wineserver -k must run even when the game itself is gone"


def test_czn_stop_for_another_window_uses_the_shared_helper(tmp_path, monkeypatch):
    backend, _killed, servers = _czn(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_game_pids", lambda helpers=True: [])
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: True)
    calls = []
    monkeypatch.setattr(
        backend_parts.launching,
        "stop_elsewhere",
        lambda p, info, **kw: calls.append(p),
        raising=False,
    )
    backend.stop()
    assert calls == [backend.prefix()]
    assert not servers  # the other window owns that wineserver


def test_czn_stop_runner_lookup_never_downloads(tmp_path, monkeypatch):
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"runner": "dwproton"}))
    monkeypatch.setattr(
        backend, "_download_runner", lambda *a, **k: pytest.fail("Stop must not download")
    )
    monkeypatch.setattr(
        backend, "_pinned_runner", lambda *a, **k: pytest.fail("Stop must not download")
    )
    assert backend._runner_for_stop() is None  # nothing installed in this fresh home


def test_czn_stop_request_from_another_window_ends_the_game(tmp_path, monkeypatch):
    backend, killed, _servers = _czn(tmp_path, monkeypatch)
    launching = backend_parts.launching
    monkeypatch.setattr(launching.sandbox, "in_flatpak", lambda: True)
    seen = {}

    def guard(prefix, on_stop):
        seen["on_stop"] = on_stop
        return contextlib.nullcontext()

    monkeypatch.setattr(launching.runlock, "guard", guard)
    pids = iter([[7], []])
    monkeypatch.setattr(backend, "_game_pids", lambda helpers=True: next(pids, []))
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    with backend._run_lock():
        seen["on_stop"]()
    assert 7 in killed
    assert backend.ctx.cancel.cancelled


# ---------------------------------------------------------------- generic Wine
def _wine(tmp_path):
    return wine_mod.WineBackend(make_context(tmp_path, "wine", {"prefix": str(tmp_path / "pfx")}))


def test_generic_wine_holds_the_run_lock_in_the_flatpak(tmp_path, monkeypatch):
    backend = _wine(tmp_path)
    monkeypatch.setattr(wine_mod.sandbox, "in_flatpak", lambda: True)
    seen = {}

    def guard(prefix, on_stop):
        seen["prefix"] = Path(prefix)
        return contextlib.nullcontext()

    monkeypatch.setattr(wine_mod.runlock, "guard", guard)
    with backend._run_lock():
        pass
    assert seen["prefix"] == backend._prefix()


def test_generic_wine_launch_runs_inside_the_run_lock(tmp_path, monkeypatch):
    backend = _wine(tmp_path)
    order = []

    @contextlib.contextmanager
    def lock():
        order.append("lock")
        yield
        order.append("unlock")

    monkeypatch.setattr(backend, "_run_lock", lock)
    monkeypatch.setattr(backend, "_launch", lambda: order.append("launch") or 0)
    assert backend.launch() == 0
    assert order == ["lock", "launch", "unlock"]


def test_generic_wine_stop_asks_another_window(tmp_path, monkeypatch):
    backend = _wine(tmp_path)
    monkeypatch.setattr(wine_mod, "scan_pids", lambda q: [])
    monkeypatch.setattr(wine_mod.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(wine_mod.runlock, "running", lambda p: True)
    calls, servers = [], []
    monkeypatch.setattr(wine_mod, "stop_elsewhere", lambda p, info, **kw: calls.append(Path(p)))
    monkeypatch.setattr(wine_mod, "stop_runner_wineserver", lambda **kw: servers.append(kw))
    backend.stop()
    assert calls == [backend._prefix()]
    assert not servers
