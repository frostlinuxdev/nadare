# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sandbox gives a game a private /tmp and PID namespace (#1593 item 5).

What still has to work inside: the X11 socket directory, the X authority file (even when it
lives in /tmp or is a link there), and the Wayland/Pulse/PipeWire sockets in
XDG_RUNTIME_DIR. Nadare itself keeps tracking the game from the host, so ``stop``, "already
running" and the launch watcher must still find it with ``--unshare-pid`` on. A per-game
profile flag exempts one game without turning the isolation off everywhere.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context

from nadare.backends import launcher, wine
from nadare.backends.winegame_parts.launching import LaunchingMixin
from nadare.core import gamesandbox, sandbox
from nadare.core.backends import BackendError


@pytest.fixture
def real_bwrap():
    binary = shutil.which("bwrap")
    if binary is None:
        pytest.skip("bubblewrap is absent")
    probe = subprocess.run(
        [binary, "--ro-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "--", "/bin/true"],
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        pytest.skip(f"bubblewrap unavailable: {probe.stderr.strip()}")
    return binary


@pytest.fixture
def prefix(tmp_path):
    folder = tmp_path / "prefix"
    folder.mkdir()
    return folder


def _flags(argv: list[str]) -> list[str]:
    return argv[: argv.index("--")]


def _run_inside(argv: list[str], env: dict[str, str], *, timeout: float = 20) -> str:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _host_marker() -> Path:
    """A file the test can see on the host but the game's private /tmp must not."""
    path = Path("/tmp") / f"nadare-1593-marker-{uuid.uuid4().hex}"
    path.write_text("host only")
    return path


def test_default_sandbox_has_a_private_tmp_and_pid_namespace(prefix, tmp_path):
    auth = tmp_path / "Xauthority"
    auth.touch()
    argv, _ = gamesandbox.wrap(["true"], {"XAUTHORITY": str(auth)}, prefix=prefix)
    flags = _flags(argv)
    assert "--unshare-pid" in flags
    assert flags[flags.index("--proc") + 1] == "/proc"
    at = flags.index("/tmp")
    assert flags[at - 1 : at + 1] == ["--tmpfs", "/tmp"]
    assert ["--ro-bind", str(auth), str(auth)] == flags[
        flags.index(str(auth)) - 1 : flags.index(str(auth)) + 2
    ]
    if Path("/tmp/.X11-unix").is_dir():
        at = flags.index("/tmp/.X11-unix")
        assert flags[at - 1 : at + 2] == [
            "--ro-bind",
            "/tmp/.X11-unix",
            "/tmp/.X11-unix",
        ]


@pytest.mark.parametrize("share_pid,host_tmp", [(True, False), (False, True), (True, True)])
def test_the_exemptions_are_independent(prefix, share_pid, host_tmp):
    argv, _ = gamesandbox.wrap(
        ["true"], {}, prefix=prefix, share_pid=share_pid, host_tmp=host_tmp
    )
    flags = _flags(argv)
    assert ("--unshare-pid" in flags) is not share_pid
    assert ("/tmp" in flags) is not host_tmp


def test_tmpdir_follows_the_tmp_choice(prefix):
    _, private = gamesandbox.wrap(["true"], {"TMPDIR": "/tmp/host-only"}, prefix=prefix)
    assert private["TMPDIR"] == "/tmp"
    _, shared = gamesandbox.wrap(
        ["true"], {"TMPDIR": "/tmp/host-only"}, prefix=prefix, host_tmp=True
    )
    assert shared["TMPDIR"] == "/tmp/host-only"


def test_real_private_tmp_and_pid_namespace(prefix, real_bwrap):
    marker = _host_marker()
    code = (
        "import os, pathlib, sys;"
        "assert not pathlib.Path(sys.argv[1]).exists(), 'host /tmp is visible';"
        "ns = os.readlink('/proc/self/ns/pid');"
        "assert pathlib.Path('/proc/self').resolve().name == str(os.getpid()), 'stale /proc';"
        "print(ns)"
    )
    argv, env = gamesandbox.wrap(
        [sys.executable, "-c", code, str(marker)],
        {**os.environ, "TMPDIR": str(marker.parent)},
        prefix=prefix,
        bwrap=real_bwrap,
    )
    try:
        assert _run_inside(argv, env).strip() != os.readlink("/proc/self/ns/pid")
    finally:
        marker.unlink()


def test_real_x11_style_socket_stays_connectable(prefix, real_bwrap):
    directory = Path("/tmp/.X11-unix")
    if not directory.is_dir():
        pytest.skip("this host has no /tmp/.X11-unix")
    name = f"X1593{uuid.uuid4().hex[:8]}"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(directory / name))
    except PermissionError:
        server.close()
        pytest.skip("this host does not let a test make a socket in /tmp/.X11-unix")
    server.listen(1)
    server.settimeout(15)
    marker = _host_marker()
    code = (
        "import pathlib,socket,sys;"
        "assert not pathlib.Path(sys.argv[2]).exists(), 'host /tmp is visible';"
        "s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.send(b'X')"
    )
    argv, env = gamesandbox.wrap(
        [sys.executable, "-c", code, str(directory / name), str(marker)],
        {**os.environ, "TMPDIR": "/tmp"},
        prefix=prefix,
        bwrap=real_bwrap,
    )
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        connection, _ = server.accept()
        with connection:
            assert connection.recv(1) == b"X"
    finally:
        server.close()
        (directory / name).unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def test_real_xauthority_in_tmp_is_readable(prefix, real_bwrap):
    auth = Path("/tmp") / f"nadare-1593-xauth-{uuid.uuid4().hex}"
    auth.write_text("cookie")
    link = Path("/tmp") / f"nadare-1593-xauth-link-{uuid.uuid4().hex}"
    link.symlink_to(auth)
    marker = _host_marker()
    code = (
        "import pathlib,sys;"
        "assert not pathlib.Path(sys.argv[2]).exists(), 'host /tmp is visible';"
        "print(pathlib.Path(sys.argv[1]).read_text())"
    )
    argv, env = gamesandbox.wrap(
        [sys.executable, "-c", code, str(link), str(marker)],
        {**os.environ, "XAUTHORITY": str(link), "TMPDIR": "/tmp"},
        prefix=prefix,
        bwrap=real_bwrap,
    )
    try:
        assert _run_inside(argv, env).strip() == "cookie"
    finally:
        auth.unlink(missing_ok=True)
        link.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def test_real_runtime_socket_survives_the_private_tmp(tmp_path, real_bwrap):
    runtime = tmp_path / "run"
    (runtime / "pulse").mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(runtime / "pulse" / "native"))
    server.listen(1)
    server.settimeout(15)
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    marker = _host_marker()
    code = (
        "import pathlib,socket,sys;"
        "assert not pathlib.Path(sys.argv[2]).exists(), 'host /tmp is visible';"
        "s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.send(b'P')"
    )
    argv, env = gamesandbox.wrap(
        [sys.executable, "-c", code, str(runtime / "pulse" / "native"), str(marker)],
        {**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
        prefix=prefix,
        bwrap=real_bwrap,
    )
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        connection, _ = server.accept()
        with connection:
            assert connection.recv(1) == b"P"
    finally:
        server.close()
        marker.unlink(missing_ok=True)


def _small_game_sandbox(directory: Path, bwrap: str) -> tuple[list[str], dict[str, str]]:
    """Build the argv/env of a long-running test game inside the Sandbox."""
    return gamesandbox.wrap(
        [sys.executable, "-c", "import time; time.sleep(30)", "supply-game"],
        {**os.environ, "WINEPREFIX": str(directory)},
        prefix=directory,
        bwrap=bwrap,
    )


def test_real_host_tracking_still_finds_and_stops_the_game(tmp_path, real_bwrap):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    query = wine.ProcessQuery(prefix=str(prefix), names=("supply-game",))
    argv, env = _small_game_sandbox(prefix, real_bwrap)
    assert "--unshare-pid" in _flags(argv)
    child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    done = threading.Event()
    events: list[object] = []
    notes: list[str] = []
    backend = SimpleNamespace(
        ctx=SimpleNamespace(cancel=SimpleNamespace(cancelled=False), emit=events.append),
        display_name="fixture",
        game_process_names=("supply-game",),
        _running_patterns=lambda: query,
        _running_elsewhere=lambda: False,
        _game_running=lambda: bool(wine.scan_pids(query)),
        info=lambda message, **data: notes.append(message),
        stop=lambda: None,
    )
    watcher = threading.Thread(
        target=LaunchingMixin._watch_game_process, args=(backend, done, threading.Event())
    )
    try:
        until = time.monotonic() + 10
        pids: list[int] = []
        while not pids and time.monotonic() < until:
            pids = wine.scan_pids(query)
            time.sleep(0.05)
        assert pids, child.poll()
        from nadare.backends.winegame import WineGameBackend

        assert WineGameBackend._game_running(backend)
        with pytest.raises(BackendError, match="Stop the game"):
            WineGameBackend._refuse_if_running(backend, "updating")
        watcher.start()
        until = time.monotonic() + 5
        while not events and time.monotonic() < until:
            time.sleep(0.05)
        assert any(getattr(event, "kind", "") == "game_running" for event in events)
        wine.stop_pids(pids)
        child.wait(timeout=10)
        assert not wine.scan_pids(query)
    finally:
        done.set()
        if watcher.ident:
            watcher.join(timeout=5)
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_profile_exemptions_reach_the_wrapper(tmp_path, monkeypatch):
    backend = launcher.NikkeBackend(
        make_context(
            tmp_path,
            launcher.NikkeBackend.kind,
            {
                "secure_mode": True,
                "extra": {"sandbox_share_pid": "true", "sandbox_host_tmp": True},
            },
        )
    )
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(
        backend, "_sandbox_check", lambda _: gamesandbox.Located(binary="bwrap")
    )
    monkeypatch.setattr(backend, "_sandbox_prefix", lambda: tmp_path / "prefix")
    monkeypatch.setattr(backend, "_sandbox_game_dir", lambda: None)
    (tmp_path / "prefix").mkdir(exist_ok=True)
    argv, _ = backend._secure_wrap(["true"], {}, links=False)
    assert "--unshare-pid" not in argv
    assert "/tmp" not in argv
    # Defaults must still be the isolated ones, so an ignored flag cannot pass this test.
    backend.ctx.profile.pop("extra")
    argv, _ = backend._secure_wrap(["true"], {}, links=False)
    assert "--unshare-pid" in argv
    assert "/tmp" in argv


def test_real_appimage_mount_in_tmp_stays_whole(prefix, real_bwrap, monkeypatch):
    """Nadare's own binary as the program (``--umu-run``, #1341) lives in the AppImage mount
    under /tmp: the whole mount must reach the private /tmp, not only the one file."""
    appdir = Path(f"/tmp/.mount_nadare-test-{uuid.uuid4().hex}")
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "lib").mkdir(parents=True)
    (appdir / "usr" / "lib" / "payload").write_text("bundled\n")
    program = appdir / "usr" / "bin" / "nadare"
    program.write_text('#!/bin/sh\nexec cat "$(dirname "$0")/../lib/payload"\n')
    program.chmod(0o755)
    monkeypatch.setenv("APPDIR", str(appdir))
    try:
        argv, env = gamesandbox.wrap(
            [str(program)], dict(os.environ), prefix=prefix, bwrap=real_bwrap
        )
        assert _run_inside(argv, env).strip() == "bundled"
        flags = _flags(argv)
        index = flags.index(str(appdir))
        assert flags[index - 1] == "--ro-bind"
    finally:
        shutil.rmtree(appdir)
