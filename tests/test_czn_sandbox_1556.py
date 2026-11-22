# SPDX-License-Identifier: GPL-3.0-or-later
"""Chaos Zero Nightmare in the Sandbox (#1556).

Opt-in for this release: an untouched CZN profile runs exactly as before. When the player
switches it on, Play runs the stub (and everything the stub starts) in one bubblewrap
instance: the prefix writable, the stub and the freshly minted session file read only, and
nothing else from the home folder. Stop, "already running" and the stub log monitor keep
working because the paths do not change inside the Sandbox.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _czn_test_helpers import (  # noqa: F401 - pytest fixtures, found by name
    _install_game,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _reset_czn_process_state,
    _webview2_ready,
)

from nadare.backends.czn import backend_parts
from nadare.backends.czn.backend import CznBackend
from nadare.core import gamesandbox, paths, protoncmd, sandbox, umu, vcredist
from nadare.core.backends import BackendError


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


@pytest.fixture(autouse=True)
def _umu_available(monkeypatch):
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(umu, "note_first_runtime", lambda emit: None)
    monkeypatch.setattr(umu, "runtime_downloaded", lambda *a, **k: True)
    monkeypatch.setattr(vcredist, "installed", lambda prefix: True)


def _proton(tmp_path: Path) -> Path:
    proton = tmp_path / "MyProton"
    proton.mkdir(exist_ok=True)
    (proton / "proton").write_text("#!/bin/sh\n")
    return proton


def _backend(tmp_path: Path, **profile) -> CznBackend:
    values = {
        "prefix": str(tmp_path / "prefix"),
        "runner": str(_proton(tmp_path)),
        **profile,
    }
    return CznBackend(make_context(tmp_path, "stove-czn", values))


def _launch(backend: CznBackend, monkeypatch, *, stream_error=None) -> dict:
    """Run CZN's Play with a stand-in stream; return the command and environment it got.

    The stub writes its launch log from inside the Sandbox; the fake stream writes the same
    file at the path the backend told it, so the real monitor reads it back on the host.
    """
    prefix = backend.prefix()
    _install_game(prefix)
    _webview2_ready(prefix)
    stub = backend._stub()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(b"MZ")
    monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
    backend._restore_attempted = True
    backend._refresh_token = "token"

    def mint_session() -> int:
        session = backend._session()
        session.write_text("{}")
        os.chmod(session, 0o600)
        return 0

    monkeypatch.setattr(backend, "_mint_session", mint_session)
    installer_calls = []
    monkeypatch.setattr(backend, "_ensure_vcredist", lambda: installer_calls.append(True))
    seen: dict = {}

    def fake_stream(cmd, env, **kwargs):
        if stream_error is not None:
            raise stream_error
        seen["cmd"] = list(cmd)
        seen["env"] = dict(env)
        seen["installer_calls"] = installer_calls
        log = backend._stub_log(sandboxed=backend.secure_mode_enabled())
        log.write_text("RESULT ok\n")
        return 0

    monkeypatch.setattr(backend, "_stream", fake_stream)
    assert backend.launch() == 0
    return seen


def _binds(argv: list[str]) -> tuple[list[Path], list[Path]]:
    """The writable and read-only bind sources in a bubblewrap command."""
    writable: list[Path] = []
    read_only: list[Path] = []
    for i, flag in enumerate(argv):
        if flag == "--bind":
            writable.append(Path(argv[i + 1]))
        elif flag == "--ro-bind":
            read_only.append(Path(argv[i + 1]))
    return writable, read_only


# ---- the switch -------------------------------------------------------------------------
def test_czn_offers_the_sandbox_but_never_turns_it_on_by_itself():
    assert CznBackend.supports_secure_mode is True
    assert CznBackend.sandbox_default_on is False
    option = next(o for o in CznBackend.options if o.key == "secure_mode")
    assert option.kind == "bool" and option.default is False
    assert "home folder" in option.help and "network" in option.help


def test_an_untouched_czn_profile_is_not_sandboxed(tmp_path, has_bwrap, monkeypatch):
    # The suite turns the global default off for older tests; turn it on so the backend's
    # own default is what decides here.
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)
    backend = _backend(tmp_path)
    assert backend.secure_mode_enabled() is False
    seen = _launch(backend, monkeypatch)
    assert Path(seen["cmd"][0]).name != "bwrap"
    assert "--ro-bind" not in seen["cmd"]
    assert seen["installer_calls"] == [True]


# ---- what reaches the Sandbox -----------------------------------------------------------
def test_secure_play_binds_the_prefix_and_the_two_files_the_launch_needs(
    tmp_path, has_bwrap, monkeypatch
):
    backend = _backend(
        tmp_path, secure_mode=True, prefix=str(Path(os.environ["HOME"]) / "My Games/CZN prefix"),
    )
    session = backend._session()
    seen = _launch(backend, monkeypatch)
    argv = seen["cmd"]
    assert Path(argv[0]).name == "bwrap" and "--tmpfs" in argv
    rest = argv[argv.index("--") + 1 :]
    stub = backend._stub().resolve()
    writable, read_only = _binds(argv)
    # The prefix (with the game and WebView2 in it) is writable.
    assert backend.prefix().resolve() in writable
    # The stub and the session file the stub is told about are the only extra binds, read only.
    assert stub in read_only
    assert session.resolve() in read_only
    # The path the game is told is the file that was bound, not a copy elsewhere.
    assert backend._win(session) in rest
    # Never the folder holding the session (and whatever else it may hold).
    assert session.parent.resolve() not in writable + read_only
    # Nothing else of Nadare's home folders, and the home itself is replaced, not bound.
    home = Path(os.environ["HOME"]).resolve()
    assert any(argv[i] == "--tmpfs" and argv[i + 1] == str(home) for i in range(len(argv) - 1))
    assert home not in writable + read_only
    assert paths.logs_dir().resolve() not in writable + read_only
    assert paths.config_dir().resolve() not in writable + read_only


def test_every_play_time_wine_process_runs_in_that_one_instance(tmp_path, has_bwrap, monkeypatch):
    """The stub starts the game and WebView2: they inherit this namespace, none runs outside."""
    backend = _backend(tmp_path, secure_mode=True)
    seen = _launch(backend, monkeypatch)
    argv = seen["cmd"]
    assert Path(argv[0]).name == "bwrap"
    rest = argv[argv.index("--") + 1 :]
    # One wrapped command carries both the stub program and the game shield it will start.
    assert str(backend._stub()) in rest
    assert backend._win(backend.shield()) in rest
    # Nothing else started Wine in the prefix: no unwrapped prefix helper ran.
    assert [part for part in rest if Path(part).name == "wineboot"] == []
    # The link relay the Sandbox started is not left running after Play.
    assert backend._link_relay is None
    assert seen["installer_calls"] == []


def test_the_stub_log_lives_in_the_prefix_so_the_monitor_still_reads_it(
    tmp_path, has_bwrap, monkeypatch
):
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    _webview2_ready(backend.prefix())
    stub = backend._stub()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(b"MZ")
    monkeypatch.setenv("CZN_SKIP_UPDATE", "1")
    backend._restore_attempted = True
    backend._refresh_token = "token"
    monkeypatch.setattr(backend, "_mint_session", lambda: 0)
    monkeypatch.setattr(backend, "_ensure_vcredist", lambda: None)
    seen: dict = {}

    def fake_stream(cmd, env, **kwargs):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(env)
        # The stub runs inside the Sandbox and writes its log there.
        backend._stub_log(sandboxed=True).write_text("banner\nRESULT ok\n")
        return 0

    monkeypatch.setattr(backend, "_stream", fake_stream)
    assert backend.launch() == 0
    log = backend._stub_log(sandboxed=True)
    assert log.is_relative_to(backend.prefix())
    assert seen["env"]["CZN_STUB_LOG"] == backend._win(log)
    # The host-side monitor reads the very file the sandboxed stub wrote.
    assert backend._stub_result(log) == "ok"


def test_prefix_command_tells_proton_the_command_runs_in_the_sandbox(tmp_path, monkeypatch):
    backend = _backend(tmp_path, secure_mode=True)
    seen: dict = {}

    def fake_proton_argv(args, **kwargs):
        seen.update(kwargs)
        return ["/usr/bin/umu-run", *args]

    monkeypatch.setattr(protoncmd, "proton_argv", fake_proton_argv)
    backend._prefix_command(["game.exe"], sandboxed=True)
    assert seen["sandboxed"] is True
    backend._prefix_command(["game.exe"])
    assert seen["sandboxed"] is False


def test_sandboxed_play_requires_repair_instead_of_starting_a_host_installer(
    tmp_path, has_bwrap, monkeypatch
):
    backend = _backend(tmp_path, secure_mode=True)
    monkeypatch.setattr(vcredist, "installed", lambda prefix: False)
    with pytest.raises(BackendError, match="Repair files"):
        _launch(backend, monkeypatch)


def test_mangohud_keeps_a_home_installed_umu_visible(tmp_path, has_bwrap, monkeypatch):
    backend = _backend(tmp_path, secure_mode=True)
    binary = Path(os.environ["HOME"]) / "My tools" / "umu-run"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: str(binary))
    monkeypatch.setenv("NADARE_MANGOHUD", "1")
    monkeypatch.setattr(
        backend_parts.launching.shutil, "which", lambda name, **kw: f"/usr/bin/{name}",
    )
    argv = _launch(backend, monkeypatch)["cmd"]
    assert binary.resolve() in _binds(argv)[1]
    assert argv[argv.index("--") + 1 :][:3] == ["/usr/bin/mangohud", "--dlsym", str(binary)]


@pytest.mark.parametrize("overlay", [False, True])
def test_flatpak_play_exposes_only_launch_files_and_waits_for_wine(
    tmp_path, monkeypatch, overlay
):
    backend = _backend(tmp_path, secure_mode=True)
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn")
    server = Path(backend.ctx.profile["runner"]) / "files/bin/wineserver"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n")
    if overlay:
        monkeypatch.setenv("NADARE_MANGOHUD", "1")
        monkeypatch.setattr(
            backend_parts.launching.shutil, "which", lambda name, **kw: f"/usr/bin/{name}",
        )
    seen = _launch(backend, monkeypatch)
    argv = seen["cmd"]
    try:
        assert "--sandbox" in argv and "--share-pids" in argv
        assert f"--sandbox-expose-path={backend.prefix().resolve()}" in argv
        for path in (backend._stub(), backend._session()):
            assert f"--sandbox-expose-path-ro={path.resolve()}" in argv
        for path in (backend._session().parent, Path(os.environ["HOME"]), paths.config_dir()):
            assert f"--sandbox-expose-path={path.resolve()}" not in argv
            assert f"--sandbox-expose-path-ro={path.resolve()}" not in argv
        assert "nadare-wait" in argv
        assert str(server) in argv
        assert "umu-run" not in [Path(part).name for part in argv]
    finally:
        # The stand-in stream never executes the wrapper that normally removes this file.
        for part in argv:
            if Path(part).name.startswith("nadare-sandbox-env-"):
                Path(part).unlink(missing_ok=True)


@pytest.mark.parametrize("failure", ["wrap", "stream"])
def test_failed_launch_removes_session_and_stops_link_relay(
    tmp_path, has_bwrap, monkeypatch, failure
):
    backend = _backend(tmp_path, secure_mode=True)
    error = BackendError("review fixture failure")
    if failure == "wrap":
        def fail_wrap(*args, **kwargs):
            backend._start_link_relay()
            raise error

        monkeypatch.setattr(backend, "_secure_wrap", fail_wrap)
    with pytest.raises(BackendError, match="review fixture failure"):
        _launch(backend, monkeypatch, stream_error=error if failure == "stream" else None)
    assert not backend._session().exists()
    assert backend._link_relay is None


# ---- sign-in stays outside --------------------------------------------------------------
def test_login_does_not_start_wine_in_the_prefix(tmp_path, monkeypatch):
    """CZN's QR sign-in is the Python auth worker; no Wine process runs, so there is no
    sandboxed sign-in window to place."""
    backend = _backend(tmp_path, secure_mode=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        backend, "_run_auth", lambda args, **_kw: (calls.append(list(args)) or 0, None)
    )

    def no_wine(*_args, **_kwargs):
        raise AssertionError("login started a Wine command")

    monkeypatch.setattr(backend, "_prefix_command", no_wine)
    backend.login()
    assert calls and calls[0][0] == "login"


# ---- stop and "already running" ---------------------------------------------------------
def _fake_sandboxed_game(game_dir: Path) -> subprocess.Popen:
    """A process whose argv looks like the game inside the Sandbox (paths do not change)."""
    exe = "Z:" + str(game_dir / "bin" / "ssr-stove-shield.exe").replace("/", "\\")
    script = "import time; time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", script, exe], start_new_session=True)
    # Popen returns when exec closes the close-on-exec pipe, which the kernel does a moment
    # before the new argv shows in /proc/PID/cmdline; on a busy CI host a scan could still
    # see the old one. Wait until the fake game is visible as such.
    cmdline = Path(f"/proc/{proc.pid}/cmdline")
    deadline = time.monotonic() + 10
    while exe.encode() not in cmdline.read_bytes() and time.monotonic() < deadline:
        time.sleep(0.01)
    return proc


def _end(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def test_a_sandboxed_game_counts_as_running(tmp_path):
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    proc = _fake_sandboxed_game(backend.game_dir())
    try:
        assert backend._game_running() is True
        assert backend._running() is True
    finally:
        _end(proc)


def test_play_refuses_while_a_sandboxed_game_is_already_running(tmp_path):
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    proc = _fake_sandboxed_game(backend.game_dir())
    try:
        with pytest.raises(BackendError) as caught:
            backend.launch()
        assert caught.value.code == 8 and "already running" in caught.value.message
    finally:
        _end(proc)


def test_stop_ends_a_sandboxed_game(tmp_path, monkeypatch):
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    proc = _fake_sandboxed_game(backend.game_dir())
    try:
        assert backend._game_running() is True
        started = time.monotonic()
        backend.stop()
        assert proc.wait(timeout=10) is not None
        assert time.monotonic() - started < 20
        assert backend._game_running() is False
    finally:
        _end(proc)


@pytest.mark.parametrize("real_path", [False, True])
def test_running_and_stop_find_a_game_in_a_symlinked_prefix(tmp_path, monkeypatch, real_path):
    real = tmp_path / "Other disk/CZN prefix"
    real.mkdir(parents=True)
    linked = Path(os.environ["HOME"]) / "CZN link"
    linked.symlink_to(real, target_is_directory=True)
    backend = _backend(tmp_path, prefix=str(linked), secure_mode=True)
    _install_game(backend.prefix())
    # Flatpak rewrites the Windows launch path to the real disk; native keeps the alias.
    game = backend.game_dir().resolve() if real_path else backend.game_dir()
    proc = _fake_sandboxed_game(game)
    monkeypatch.setattr(backend, "_runner_for_stop", lambda: None)
    try:
        assert backend._game_running() is True
        assert backend._running() is True
        backend.stop()
        assert proc.wait(timeout=10) is not None
        assert backend._game_running() is False
    finally:
        _end(proc)


def test_stop_kills_a_sandboxed_process_that_ignores_sigterm(tmp_path, monkeypatch):
    """Wine helpers ignore SIGTERM; the Sandbox's own wineserver cannot be reached from the
    app, so whatever is left after the first sweep is SIGKILLed (as on the Flatpak rig)."""
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    exe = "Z:" + str(backend.game_dir() / "bin" / "ssr-stove-shield.exe").replace("/", "\\")
    script = (
        "import signal, time\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"
        "print('ready', flush=True)\ntime.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, exe], start_new_session=True, stdout=subprocess.PIPE,
    )
    monkeypatch.setattr(backend, "_runner_for_stop", lambda: None)
    try:
        assert proc.stdout.readline() == b"ready\n"
        # Advance the grace period without a ten-second wait; the handler is installed.
        ticks = iter([0, 0, 11])
        monkeypatch.setattr(backend_parts.launching.time, "time", lambda: next(ticks, 11))
        assert backend._game_running() is True
        backend.stop()
        assert proc.wait(timeout=10) is not None
        assert backend._game_running() is False
    finally:
        _end(proc)
        proc.stdout.close()


def test_stop_ends_sandboxed_wine_helpers_outside_the_game_bin(tmp_path, monkeypatch):
    backend = _backend(tmp_path, secure_mode=True)
    _install_game(backend.prefix())
    helper = backend.prefix() / "drive_c/Program Files/WebView2/msedgewebview2.exe"
    script = (
        "import signal, time\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"
        "print('ready', flush=True)\ntime.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, backend._win(helper)],
        env={**os.environ, "WINEPREFIX": str(backend.prefix() / "pfx")},
        start_new_session=True, stdout=subprocess.PIPE,
    )
    monkeypatch.setattr(backend, "_runner_for_stop", lambda: None)
    try:
        assert proc.stdout.readline() == b"ready\n"
        # A helper alone must not say the game is running, but Stop still ends it.
        assert backend._game_running() is False
        backend.stop()
        assert proc.wait(timeout=1) is not None
    finally:
        _end(proc)
        proc.stdout.close()
