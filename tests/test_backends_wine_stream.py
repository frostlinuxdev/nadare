# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Behavioural tests for the shared Wine/Proton subprocess streaming.

These use real subprocesses (no Wine): a ``python`` child for cancellation and
a shell that leaves a grandchild holding the stdout pipe. Wine itself is never
started; the wineserver hook and the runner call are monkeypatched.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import wine as wine_mod
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.steam import SteamBackend
from nadare.backends.wine import ProcessQuery, WineBackend
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled


def _silent_child(pidfile: Path, seconds: int = 30) -> list[str]:
    """A python child that records its pid and then prints nothing."""
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        f"time.sleep({seconds})"
    )
    return [sys.executable, "-c", script]


def _cancel_after(cancel: Cancel, delay: float = 0.3) -> threading.Thread:
    def _run() -> None:
        time.sleep(delay)
        cancel.cancel()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _process_gone(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _read_pid(pidfile: Path, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.is_file():
            text = pidfile.read_text().strip()
            if text:
                return int(text)
        time.sleep(0.05)
    raise AssertionError("child never wrote its pid")


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------
def test_stream_cancel_reaches_a_silent_child(tmp_path, monkeypatch):
    monkeypatch.setattr(wine_mod, "_stop_wineserver", lambda *_a, **_k: None)
    pidfile = tmp_path / "pid"
    cancel = Cancel()
    _cancel_after(cancel, 0.3)

    started = time.monotonic()
    with pytest.raises(Cancelled):
        wine_mod.stream(_silent_child(pidfile), dict(os.environ), cancel=cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert _process_gone(_read_pid(pidfile))


def test_stream_returns_when_child_exits_but_grandchild_holds_pipe(monkeypatch):
    monkeypatch.setattr(wine_mod, "_stop_wineserver", lambda *_a, **_k: None)
    lines: list[str] = []
    started = time.monotonic()
    code = wine_mod.stream(
        ["sh", "-c", "sleep 30 & echo $!"],
        dict(os.environ),
        on_line=lines.append,
    )
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 3.0
    assert lines, "expected the child's output"
    grandchild = int(lines[0])
    try:
        os.kill(grandchild, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert alive, "a normal exit must not kill the grandchild"


def test_stream_cancel_invokes_the_wineserver_hook(monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        wine_mod,
        "_stop_wineserver",
        lambda argv, env: calls.append((list(argv), dict(env))),
    )
    cancel = Cancel()
    _cancel_after(cancel, 0.2)

    with pytest.raises(Cancelled):
        wine_mod.stream(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            {"WINEPREFIX": "/games/pfx"},
            cancel=cancel,
        )

    assert calls, "wineserver -k was not called on cancel"
    argv, env = calls[0]
    assert argv[0] == sys.executable
    assert env["WINEPREFIX"] == "/games/pfx"


def test_wineserver_for_bare_wine():
    ws, prefix = wine_mod._wineserver_for(
        ["/opt/wine/bin/wine", "game.exe"], {"WINEPREFIX": "/games/pfx"}
    )
    assert ws == Path("/opt/wine/bin/wineserver")
    assert prefix == Path("/games/pfx")


def test_wineserver_for_proton_runner():
    ws, prefix = wine_mod._wineserver_for(
        ["/usr/bin/umu-run", "game.exe"],
        {"PROTONPATH": "/runners/Proton-CachyOS", "WINEPREFIX": "/games/pfx"},
    )
    assert ws == Path("/runners/Proton-CachyOS/files/bin/wineserver")
    assert prefix == Path("/games/pfx")


def test_stop_wineserver_runs_k(tmp_path, monkeypatch):
    ws = tmp_path / "wineserver"
    ws.write_text("#!/bin/sh\n")
    ws.chmod(0o755)
    calls: list[tuple[list[str], dict[str, str]]] = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return _Result()

    monkeypatch.setattr(wine_mod.subprocess, "run", fake_run)
    wine_mod._stop_wineserver(
        [str(ws), "game.exe"], {"WINEPREFIX": "/games/pfx", "PATH": "/usr/bin"}
    )

    assert calls
    argv, env = calls[0]
    assert argv == [str(ws), "-k"]
    assert env["WINEPREFIX"] == "/games/pfx"


# ---------------------------------------------------------------------------
# process detection: prefix scoping and exact-argv matching (05-2 / 05-3)
# ---------------------------------------------------------------------------
def _fake_pid(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is not None:
        blob = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
        (entry / "environ").write_bytes(blob)


def test_name_match_is_ignored_for_a_process_in_another_prefix(tmp_path):
    root = tmp_path / "proc"
    _fake_pid(root, 800, ["C:/games/game.exe"], {"WINEPREFIX": "/other/pfx"})
    query = ProcessQuery(prefix="/games/pfx", names=("game.exe",))
    assert wine_mod.scan_pids(query, proc_root=root) == []


def test_name_match_still_works_without_a_wine_environment(tmp_path):
    root = tmp_path / "proc"
    _fake_pid(root, 801, ["/games/pfx/drive_c/game.exe"])
    query = ProcessQuery(prefix="/games/pfx", names=("game.exe",))
    assert wine_mod.scan_pids(query, proc_root=root) == [801]


def test_exact_argv_element_matches_but_bare_substring_does_not(tmp_path):
    root = tmp_path / "proc"
    _fake_pid(root, 900, ["/usr/bin/steam", "steam://rungameid/730"])
    _fake_pid(root, 901, ["/usr/bin/tail", "-f", "730"])
    query = ProcessQuery(args=("steam://rungameid/730",))
    assert wine_mod.scan_pids(query, proc_root=root) == [900]


def test_steam_matches_the_real_launch_forms(tmp_path):
    """N1: Steam re-launches the game as ``reaper … AppId=<id>`` and a manual
    launch is ``steam -applaunch <id>``; neither carries the launch URI."""
    root = tmp_path / "proc"
    _fake_pid(
        root,
        920,
        [
            "/home/u/.steam/root/ubuntu12_32/reaper",
            "SteamLaunch",
            "AppId=730",
            "--",
            "/home/u/.steam/root/ubuntu12_32/steam-launch-wrapper",
            "--",
            "/home/u/.steam/root/steamapps/common/Proton/proton",
            "run",
            "game.exe",
        ],
    )
    _fake_pid(root, 921, ["/usr/bin/steam", "-applaunch", "730"])
    _fake_pid(root, 922, ["/usr/bin/steam", "steam://rungameid/730"])
    # A longer, unrelated app id must not be mistaken for the target app, and a
    # bare numeric argument must never count.
    _fake_pid(root, 923, ["/usr/bin/steam", "-applaunch", "73000"])
    _fake_pid(root, 924, ["/usr/bin/tail", "-f", "730"])

    ctx = make_context(tmp_path, "steam", {"steam_app_id": "730"})
    pids = SteamBackend(ctx)._matching_pids("730", proc_root=root)
    assert set(pids) == {920, 921, 922}


def test_stop_uses_the_runner_local_wineserver(tmp_path, monkeypatch):
    """R2-F2: stop() must not fall back to an unrelated wineserver on PATH."""
    runner = tmp_path / "Proton-CachyOS"
    (runner / "files" / "bin").mkdir(parents=True)
    (runner / "proton").write_text("")
    ws = runner / "files" / "bin" / "wineserver"
    ws.write_text("#!/bin/sh\n")
    ws.chmod(0o755)
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")

    ctx = make_context(
        tmp_path,
        "wine",
        {
            "install_dir": str(game),
            "exe_name": "game.exe",
            "prefix": str(tmp_path / "prefix"),
            "proton_runner": str(runner),
        },
    )
    calls: list[tuple[list[str], dict[str, str]]] = []

    class _Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return _Result()

    monkeypatch.setattr(wine_mod.subprocess, "run", fake_run)
    WineBackend(ctx).stop()

    assert calls
    argv, env = calls[0]
    assert argv == [str(ws), "-k"]
    assert env["PROTONPATH"] == str(runner)


# ---------------------------------------------------------------------------
# Arknights installer honesty
# ---------------------------------------------------------------------------
def _installer_backend(tmp_path: Path) -> tuple[ArknightsBackend, list]:
    backend = ArknightsBackend(make_context(tmp_path, ak.KIND, {}, "ak"))
    events: list = []
    backend.ctx.emit = events.append
    return backend, events


def _wire_install(monkeypatch, backend: ArknightsBackend, installer: Path, code: int) -> None:
    monkeypatch.setattr(ak, "stream", lambda *a, **k: code)
    monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda self, url: installer)
    backend.ctx.profile["installer_url"] = (
        "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe"
    )
    backend.ctx.profile["wine_binary"] = "/usr/bin/wine"


def test_install_without_launcher_is_an_error(tmp_path, monkeypatch):
    backend, events = _installer_backend(tmp_path)
    installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
    _wire_install(monkeypatch, backend, installer, 0)

    with pytest.raises(BackendError) as excinfo:
        backend.install()

    message = str(excinfo.value)
    assert "was not installed" in message
    assert "exit code 0" in message
    assert not any(e.kind == "done" for e in events)


def test_install_with_launcher_emits_done(tmp_path, monkeypatch):
    backend, events = _installer_backend(tmp_path)
    installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
    _wire_install(monkeypatch, backend, installer, 0)
    write_exe(backend.install_dir() / "game launcher.exe", "MZ")

    backend.install()

    assert any(e.kind == "done" for e in events)


def test_install_emits_finish_window_step(tmp_path, monkeypatch):
    backend, events = _installer_backend(tmp_path)
    installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
    _wire_install(monkeypatch, backend, installer, 0)
    write_exe(backend.install_dir() / "game launcher.exe", "MZ")

    backend.install()

    assert any(e.kind == "step" and "Finish the installer window" in e.message for e in events)
