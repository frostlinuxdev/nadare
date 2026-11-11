# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: Chaos Zero Nightmare in another instance also counts as running."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runlock, sandbox

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _holder(prefix: Path, *, obey_stop: bool = False) -> subprocess.Popen:
    """Another process (another Flatpak instance) holding the run lock."""
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {SRC!r})
        from nadare.core import runlock
        with runlock.hold({str(prefix)!r}):
            print("held", flush=True)
            for _ in range(600):
                if {obey_stop!r} and runlock.stop_requested({str(prefix)!r}):
                    break
                time.sleep(0.05)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _end(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)


def _backend(tmp_path: Path) -> CznBackend:
    ctx = make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    return CznBackend(ctx)


def test_in_flatpak_a_game_launched_elsewhere_shows_as_running(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix())
    try:
        status = backend.status()
        assert status.running is True
        assert status.data["running_elsewhere"] is True
    finally:
        _end(proc)
    assert backend.status().running is False


def test_in_flatpak_update_refuses_while_the_game_runs_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix())
    try:
        with pytest.raises(Exception, match="running"):
            backend.update()
    finally:
        _end(proc)


def test_in_flatpak_stop_asks_the_other_instance_and_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix(), obey_stop=True)
    try:
        started = time.monotonic()
        backend.stop()
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - started < 10
        assert runlock.running(backend.prefix()) is False
    finally:
        if proc.poll() is None:
            _end(proc)


def test_in_flatpak_launch_holds_the_lock_and_obeys_a_stop_request(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "installed", lambda: True)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "_restore_attempted", True)
    monkeypatch.setattr(backend, "_refresh_token", "token")
    monkeypatch.setattr(backend, "_mint_session", lambda: 0)
    monkeypatch.setattr(backend, "_cleanup_stale_session", lambda: None)
    monkeypatch.setattr(backend, "_ensure_stub", lambda: tmp_path / "stub")
    monkeypatch.setattr(backend, "_ensure_vcredist", lambda: None)
    monkeypatch.setattr(backend, "_prefix_command", lambda args, extra=None: (["stub"], {}))
    monkeypatch.setattr(backend, "_stub_result", lambda log: "ok")
    seen = {}

    def fake_stream(cmd, env, **kwargs):
        seen["held"] = runlock.running(backend.prefix())
        runlock.request_stop(backend.prefix())
        deadline = time.monotonic() + 10
        while not backend.ctx.cancel.cancelled and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["cancelled"] = backend.ctx.cancel.cancelled
        return 0

    monkeypatch.setattr(backend, "_stream", fake_stream)
    try:
        backend.launch()
    except Exception:  # a cancelled launch may raise; either way it must stop
        pass
    assert seen.get("held") is True
    assert seen.get("cancelled") is True
    assert runlock.running(backend.prefix()) is False
