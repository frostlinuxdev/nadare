# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: a game started by another Nadare instance still counts as running.

Every ``flatpak run`` gets its own process namespace, so a game started from a desktop shortcut
(``nadare launch -p X``, its own instance) is invisible in ``/proc`` to the main window, which
could then update or repair the game under it. The instance that launches holds a lock file for
the whole launch; other instances see the lock (flock works across namespaces on the same file),
report the game as running, refuse to change its files, and ask the owner to stop it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.backends import winegame as wg
from nadare.backends.winegame import WineGameBackend
from nadare.core import runlock, sandbox
from nadare.core.backends import BackendError

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _holder(prefix: Path, tmp_path: Path, *, obey_stop: bool = False) -> subprocess.Popen:
    """Another process (standing in for another Flatpak instance) that holds the run lock."""
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
    import os

    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _end(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)


# ---- the lock itself ---------------------------------------------------------------------------
def test_nobody_holding_means_not_running(tmp_path):
    assert runlock.running(tmp_path / "prefix") is False


def test_a_lock_held_by_another_process_means_running(tmp_path):
    prefix = tmp_path / "prefix"
    proc = _holder(prefix, tmp_path)
    try:
        assert runlock.running(prefix) is True
    finally:
        _end(proc)
    assert runlock.running(prefix) is False


def test_the_same_prefix_spelled_differently_is_one_lock(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    proc = _holder(prefix, tmp_path)
    try:
        assert runlock.running(tmp_path / "x" / ".." / "prefix") is True
    finally:
        _end(proc)


def test_a_stop_request_reaches_the_holder_and_is_cleared_after(tmp_path):
    prefix = tmp_path / "prefix"
    with runlock.hold(prefix):
        assert runlock.stop_requested(prefix) is False
        runlock.request_stop(prefix)
        assert runlock.stop_requested(prefix) is True
    assert runlock.stop_requested(prefix) is False
    assert runlock.running(prefix) is False


def test_the_lock_lives_outside_the_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    with runlock.hold(prefix):
        assert list(prefix.iterdir()) == []


# ---- the backend -------------------------------------------------------------------------------
def _backend(tmp_path) -> WineGameBackend:
    """A real game backend (NIKKE's publisher launcher) with a plain Wine runner."""
    from nadare.backends.launcher import NikkeBackend

    wine = write_exe(tmp_path / "wine")
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    ctx = make_context(
        tmp_path,
        "launcher-nikke",
        {"wine_binary": str(wine), "prefix": str(tmp_path / "prefix"), "secure_mode": False},
    )
    return NikkeBackend(ctx)


def test_in_flatpak_a_game_launched_elsewhere_shows_as_running(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix(), tmp_path)
    try:
        assert backend.status().running is True
    finally:
        _end(proc)
    assert backend.status().running is False


def test_in_flatpak_files_are_not_replaced_under_a_game_running_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix(), tmp_path)
    try:
        with pytest.raises(BackendError, match="Stop the game"):
            backend._refuse_if_running("updating it")
    finally:
        _end(proc)


def test_outside_flatpak_nothing_changes(tmp_path, monkeypatch):
    """The AppImage sees every process in /proc already; the lock is not consulted there."""
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix(), tmp_path)
    try:
        assert backend.status().running is False
    finally:
        _end(proc)


def test_in_flatpak_stop_asks_the_other_instance_and_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    backend = _backend(tmp_path)
    proc = _holder(backend.prefix(), tmp_path, obey_stop=True)
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
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None)
    seen = {}

    def fake_stream(argv, env, on_line=None, cancel=None, **_kw):
        seen["held"] = runlock.running(backend.prefix())
        runlock.request_stop(backend.prefix())
        deadline = time.monotonic() + 10
        while not cancel.cancelled and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["cancelled"] = cancel.cancelled
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    try:
        backend.launch()
    except Exception:  # a cancelled launch may raise Cancelled; either way it must stop
        pass
    assert seen.get("held") is True
    assert seen.get("cancelled") is True
    assert runlock.running(backend.prefix()) is False


# ---- prefix setup waits for Wine to finish (Flatpak runs Proton without umu) ---------------------
def _proton_backend(tmp_path, monkeypatch, *, flatpak: bool):
    proton = tmp_path / "runner"
    write_exe(proton / "proton")
    record = tmp_path / "wineserver-calls.txt"
    write_exe(
        proton / "files" / "bin" / "wineserver",
        f'#!/bin/sh\necho "$* WINEPREFIX=$WINEPREFIX" >> {record}\n',
    )
    from nadare.backends.launcher import NikkeBackend

    ctx = make_context(
        tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix"), "secure_mode": False}
    )
    backend = NikkeBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (proton, "proton"))
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: flatpak)
    # The direct path: no umu (sandboxed runs in the Flatpak, or no umu at all).
    monkeypatch.setattr(wg.umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(wg.umu, "resolve", lambda *_a, **_k: None)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda *_a, **_k: [])

    def fake_stream(argv, env, **_kw):
        if list(argv)[1:2] == ["run"]:
            (tmp_path / "prefix" / "drive_c").mkdir(parents=True, exist_ok=True)
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    return backend, record


def test_in_flatpak_prefix_creation_waits_for_the_wineserver(tmp_path, monkeypatch):
    """`proton run wineboot -u` returns while wineboot still works; the next step then saw
    the prefix's Wine processes as "the game is running" and refused to install."""
    backend, record = _proton_backend(tmp_path, monkeypatch, flatpak=True)
    backend._ensure_prefix()
    calls = record.read_text().splitlines() if record.exists() else []
    assert any(line.startswith("-w ") for line in calls), calls
    # Proton keeps the Wine prefix in <prefix>/pfx.
    assert any(line.rstrip("/").endswith(str(tmp_path / "prefix" / "pfx")) for line in calls), calls


def test_commands_run_in_the_prefix_wait_too(tmp_path, monkeypatch):
    backend, record = _proton_backend(tmp_path, monkeypatch, flatpak=True)
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
    backend._run_in_prefix(["vc_redist.x64.exe", "/quiet"])
    calls = record.read_text().splitlines() if record.exists() else []
    assert any(line.startswith("-w ") for line in calls), calls


def test_a_direct_download_game_holds_the_lock_too(tmp_path, monkeypatch):
    """Direct games override launch(); the lock must sit where the game runs (found on a real
    Flatpak: `nadare stop` from another instance said "not running" for GFL2)."""
    from nadare.backends.direct import BrownDust2Backend

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    wine = write_exe(tmp_path / "wine")
    game = tmp_path / "game"
    exe = write_exe(game / "BrownDust II.exe", "MZ")
    ctx = make_context(
        tmp_path,
        "direct-bd2",
        {
            "wine_binary": str(wine),
            "prefix": str(tmp_path / "prefix"),
            "install_dir": str(game),
            "secure_mode": False,
        },
    )
    backend = BrownDust2Backend(ctx)
    monkeypatch.setattr(backend, "direct_target", lambda: exe)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None)
    seen = {}

    def fake_stream(argv, env, on_line=None, cancel=None, **_kw):
        seen["held"] = runlock.running(backend.prefix())
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    backend.launch()
    assert seen.get("held") is True
    assert runlock.running(backend.prefix()) is False


def test_a_stop_request_left_by_a_crash_does_not_stop_the_next_launch(tmp_path):
    """Sweep b23r1 c20: a stop file left when the holder was killed (no clean-up) stopped
    the next game as soon as it started."""
    prefix = tmp_path / "prefix"
    runlock.request_stop(prefix)  # nobody held the lock: a leftover
    with runlock.hold(prefix):
        assert runlock.stop_requested(prefix) is False
