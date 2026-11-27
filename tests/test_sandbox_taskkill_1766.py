# SPDX-License-Identifier: GPL-3.0-or-later
"""#1766: Stop asks a Sandboxed game to close with the same ``taskkill`` as any other game.

The game's wineserver lives in the Sandbox's private ``/tmp`` and PID namespace, so a
``taskkill`` started outside would meet no server and start a second one. The launch saves
the command as it runs *inside* the Sandbox, and Stop runs it through ``nsenter`` in the
Sandbox's own namespaces (no root: the player owns them). That works on X11 and native
Wayland alike; the X11 window request stays only for when the Sandbox cannot be joined.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context
from test_stop_grace_1421 import (
    _backend,
    _fake_clock,
    _fake_x11,
    _patch_stop,
)

from nadare.backends import launcher
from nadare.backends.winegame_parts import launching
from nadare.core import gamesandbox, sandbox, sandboxjoin, stopstate

BWRAP = ["bwrap", "--wrapped-flags", "--"]


def _record(backend, inside_argv, inside_env, *, outer=None, game_argc=1):
    backend._sandbox_wrapped = True
    backend._sandbox_inside = (list(inside_argv), dict(inside_env))
    argv = outer if outer is not None else [*BWRAP, *inside_argv]
    backend._record_polite_stop(argv, dict(inside_env), game_argc)
    return stopstate.load(backend.prefix())


# ---- the launch saves the command as it runs inside --------------------------------------


def test_a_sandboxed_launch_saves_the_inside_taskkill(tmp_path, monkeypatch):
    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "_stop_exe_names", lambda: ["nikke_launcher.exe", "nikke.exe"])
    saved = _record(backend, ["/r/umu-run", "nikke_launcher.exe"], {"WINEPREFIX": "/p"})

    assert saved is not None and saved["kind"] == "taskkill"
    assert saved["inside"] is True
    assert saved["commands"] == [
        ["/r/umu-run", "taskkill", "/IM", "nikke_launcher.exe"],
        ["/r/umu-run", "taskkill", "/IM", "nikke.exe"],
    ]
    assert all("bwrap" not in argv for argv in saved["commands"]), (
        "a second bwrap would make a second Sandbox with a second wineserver"
    )
    assert saved["env"] == {"WINEPREFIX": "/p"}


def test_loader_variables_reach_the_inside_command_not_nsenter(tmp_path, monkeypatch):
    # bwrap passes LD_* with --setenv so the host-side bwrap never loads them; nsenter is a
    # host program too, so they go on the command line of `env`, which runs inside.
    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "_stop_exe_names", lambda: ["nikke_launcher.exe"])
    saved = _record(
        backend,
        ["/r/umu-run", "nikke_launcher.exe"],
        {"WINEPREFIX": "/p", "LD_PRELOAD": "/x/hook.so"},
    )

    assert saved["commands"] == [
        ["env", "LD_PRELOAD=/x/hook.so", "/r/umu-run", "taskkill", "/IM", "nikke_launcher.exe"]
    ]
    assert "LD_PRELOAD" not in saved["env"]


def test_a_wrapped_command_that_does_not_end_with_the_inside_one_falls_back(tmp_path, monkeypatch):
    backend, _events = _backend(tmp_path)
    monkeypatch.setattr(backend, "_stop_exe_names", lambda: ["nikke_launcher.exe"])
    saved = _record(
        backend,
        ["/r/umu-run", "nikke_launcher.exe"],
        {},
        outer=["something", "else", "nikke_launcher.exe"],
    )
    assert saved["kind"] == "x11" and saved["inside"] is False


def test_a_sandbox_that_cannot_be_joined_still_saves_the_x11_request(tmp_path, monkeypatch):
    # The Flatpak's sub-sandbox: Nadare saw no inside command.
    backend, _events = _backend(tmp_path)
    backend._sandbox_wrapped = True
    backend._sandbox_inside = None
    backend._record_polite_stop([*BWRAP, "game.exe"], {}, 1)
    saved = stopstate.load(backend.prefix())
    assert saved["kind"] == "x11" and saved["inside"] is False


def test_the_bwrap_wrap_remembers_the_inside_command(tmp_path, monkeypatch):
    backend = launcher.NikkeBackend(
        make_context(tmp_path, launcher.NikkeBackend.kind, {"secure_mode": True})
    )
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(backend, "_sandbox_check", lambda _: gamesandbox.Located(binary="bwrap"))
    monkeypatch.setattr(backend, "_sandbox_prefix", lambda: tmp_path / "prefix")
    monkeypatch.setattr(backend, "_sandbox_game_dir", lambda: None)
    (tmp_path / "prefix").mkdir(exist_ok=True)
    env = {"WINEPREFIX": str(tmp_path / "prefix"), "LD_LIBRARY_PATH": "/lib/x"}

    argv, wrapped_env = backend._secure_wrap(["true", "game.exe"], env, links=False)

    inside_argv, inside_env = backend._sandbox_inside
    assert inside_argv == ["true", "game.exe"] == argv[-2:]
    assert inside_env["LD_LIBRARY_PATH"] == "/lib/x", "the --setenv variables count inside"
    assert "LD_LIBRARY_PATH" not in wrapped_env
    assert inside_env["TMPDIR"] == "/tmp"


def test_an_unsandboxed_wrap_forgets_an_earlier_inside_command(tmp_path, monkeypatch):
    backend = launcher.NikkeBackend(
        make_context(tmp_path, launcher.NikkeBackend.kind, {"secure_mode": False})
    )
    backend._sandbox_inside = (["old"], {})
    backend._secure_wrap(["true"], {}, links=False)
    assert backend._sandbox_inside is None


def test_the_state_file_keeps_inside_and_an_older_file_is_ignored(tmp_path):
    prefix = tmp_path / "pfx"
    stopstate.save(prefix, kind="taskkill", commands=[["umu-run", "taskkill"]], inside=True)
    assert stopstate.load(prefix)["inside"] is True
    stopstate.save(prefix, kind="taskkill", commands=[["wine", "taskkill"]])
    assert stopstate.load(prefix)["inside"] is False
    # An older Nadare's file has no "inside": running its taskkill outside is the old rule,
    # so it is not guessed at (and an older Nadare ignores ours).
    stopstate.path(prefix).write_text(
        '{"version": 1, "kind": "taskkill", "commands": [["w", "t"]], "env": {}}'
    )
    assert stopstate.load(prefix) is None


# ---- Stop runs it inside -----------------------------------------------------------------


def _stop_setup(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path)
    clock = [100.0]
    _fake_clock(monkeypatch, clock)
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    stopstate.save(
        backend.prefix(),
        kind="taskkill",
        commands=[["/r/umu-run", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={"WINEPREFIX": "/p"},
        inside=True,
    )
    return backend, events, clock, alive, calls


def _real_time_stop_setup(tmp_path, monkeypatch, *, grace=0.4):
    """Like :func:`_stop_setup`, but a real short grace: a helper must really run or hang."""
    backend, events = _backend(tmp_path)
    backend.ctx.profile["stop_grace_seconds"] = grace
    alive = {7}
    calls: list[tuple[str, list[int]]] = []
    _patch_stop(monkeypatch, alive, calls)
    stopstate.save(
        backend.prefix(),
        kind="taskkill",
        commands=[["/r/umu-run", "taskkill", "/IM", "nikke_launcher.exe"]],
        env={"WINEPREFIX": "/p"},
        inside=True,
    )
    return backend, events, alive, calls


def test_the_shared_stop_fixture_never_reads_this_hosts_proc(tmp_path, monkeypatch):
    """Every stop test with fake pids gets an empty environment, not this process's."""
    _stop_setup(tmp_path, monkeypatch)
    assert launching.environ_of(os.getpid()) == {}


def test_stop_runs_the_saved_taskkill_inside_the_sandbox(tmp_path, monkeypatch):
    backend, _events, clock, alive, calls = _stop_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: ["nsenter", "-t", "5", "-m", "--"])
    sent: list[list[int]] = []
    _fake_x11(monkeypatch, [(11, 7)], sent)
    ran: list[list[str]] = []

    def helper(argv, env, _deadline):
        ran.append(list(argv))
        assert env["WINEPREFIX"] == "/p"
        alive.clear()  # the game closed
        return True

    monkeypatch.setattr(backend, "_run_stop_helper", helper)

    backend.stop()

    assert ran == [
        ["nsenter", "-t", "5", "-m", "--", "/r/umu-run", "taskkill", "/IM", "nikke_launcher.exe"]
    ]
    assert sent == [], "with the Sandbox joined, no X11 request is needed"
    assert calls == [], "a game that closed is not signalled"


def test_a_sandbox_that_cannot_be_joined_at_stop_uses_the_window_request(tmp_path, monkeypatch):
    backend, events, clock, _alive, calls = _stop_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: None)
    monkeypatch.setenv("DISPLAY", ":0")
    sent: list[list[int]] = []
    x11 = _fake_x11(monkeypatch, [(11, 7)], sent)
    monkeypatch.setattr(x11, "client_pids", lambda windows: {})
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda *_a, **_k: pytest.fail("no taskkill may run outside the Sandbox"),
    )

    backend.stop()

    assert sent == [[11]]
    assert calls == [("term", [7])]
    logged = [e.message for e in events if e.kind == "log"]
    assert any("could not join the game's Sandbox" in m for m in logged), logged


def test_a_stop_without_a_display_asks_a_joinable_sandbox_inside(tmp_path, monkeypatch):
    """``nadare stop`` from a terminal has no DISPLAY; a joined Sandbox needs none."""
    backend, _events, _clock, alive, calls = _stop_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: ["nsenter", "-t", "5", "-m", "--"])
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: pytest.fail("a joinable Sandbox must not need the game's display"),
    )
    ran: list[list[str]] = []

    def helper(argv, _env, _deadline):
        ran.append(list(argv))
        alive.clear()  # the game closed
        return True

    monkeypatch.setattr(backend, "_run_stop_helper", helper)

    backend.stop()

    assert ran and ran[0][:2] == ["nsenter", "-t"]
    assert calls == [], "the game closed inside its Sandbox, so nothing is signalled"


def test_a_stop_without_a_display_borrows_the_games_display_when_joining_fails(
    tmp_path, monkeypatch
):
    """The CLI has no DISPLAY, but the game's own environment names its session's server."""
    backend, _events, _clock, _alive, calls = _stop_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    xauthority = tmp_path / "xauth"
    xauthority.write_text("")
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": str(xauthority)},
    )
    sent: list[list[int]] = []
    x11 = _fake_x11(monkeypatch, [(11, 7)], sent)
    seen: list[tuple[str | None, str | None]] = []

    def windows():
        seen.append((os.environ.get("DISPLAY"), os.environ.get("XAUTHORITY")))
        return [(11, 7)]

    monkeypatch.setattr(x11, "client_pids", lambda _windows: {})
    monkeypatch.setattr(x11, "list_windows_owned", windows)
    monkeypatch.setattr(
        backend,
        "_run_stop_helper",
        lambda *_a, **_k: pytest.fail("no taskkill may run outside the Sandbox"),
    )

    backend.stop()

    assert seen == [(":7", str(xauthority))]
    assert sent == [[11]]
    assert calls == [("term", [7])]
    assert os.environ.get("DISPLAY") is None, "the borrowed display must be given back"
    assert os.environ.get("XAUTHORITY") is None
    logged = [e.message for e in _events if e.kind == "log"]
    assert any("could not join the game's Sandbox" in m for m in logged), logged


def test_a_join_that_fails_at_runtime_still_asks_the_windows(tmp_path, monkeypatch):
    """AppArmor can refuse ``nsenter`` (exit 1); Stop must still ask the game's windows."""
    backend, events, _alive, calls = _real_time_stop_setup(tmp_path, monkeypatch)
    # nsenter's own exit 1, as an AppArmor refusal gives; the saved taskkill never runs.
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: ["sh", "-c", "exit 1"])
    monkeypatch.setenv("DISPLAY", ":0")
    sent: list[list[int]] = []
    x11 = _fake_x11(monkeypatch, [(11, 7)], sent)
    monkeypatch.setattr(x11, "client_pids", lambda _windows: {})

    backend.stop()

    assert sent == [[11]], "a failed join must fall back to the window request"
    assert calls == [("term", [7])]
    logged = [e.message for e in events if e.kind == "log"]
    assert any("windows instead" in m for m in logged), logged


def test_a_taskkill_that_times_out_still_asks_the_windows(tmp_path, monkeypatch):
    """The helper inside can hang; Stop must not go straight to SIGTERM for that either."""
    backend, events, _alive, calls = _real_time_stop_setup(tmp_path, monkeypatch, grace=0.5)
    monkeypatch.setattr(launching, "_STOP_HELPER_TIMEOUT", 0.15)
    monkeypatch.setattr(sandboxjoin, "enter_argv", lambda pids: ["sleep", "30"])
    monkeypatch.setenv("DISPLAY", ":0")
    sent: list[list[int]] = []
    x11 = _fake_x11(monkeypatch, [(11, 7)], sent)
    monkeypatch.setattr(x11, "client_pids", lambda _windows: {})

    backend.stop()

    assert sent == [[11]], "a timed-out helper must fall back to the window request"
    assert calls == [("term", [7])]
    logged = [e.message for e in events if e.kind == "log"]
    assert any("windows instead" in m for m in logged), logged


# ---- borrowing the game's display --------------------------------------------------------


def test_a_borrowed_display_is_restored_when_an_x11_helper_raises(tmp_path, monkeypatch):
    xauthority = tmp_path / "xauth"
    xauthority.write_text("")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": str(xauthority)},
    )

    with pytest.raises(RuntimeError):
        with launching._game_display([7]) as borrowed:
            assert borrowed is True
            assert os.environ["DISPLAY"] == ":7"
            assert os.environ["XAUTHORITY"] == str(xauthority)
            raise RuntimeError("XOpenDisplay failed")

    assert os.environ.get("DISPLAY") is None
    assert os.environ.get("XAUTHORITY") is None


def test_an_existing_display_is_never_overridden(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/host/xauth")
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: pytest.fail("a display is already set; nothing may be borrowed"),
    )

    with launching._game_display([7]) as borrowed:
        assert borrowed is False
        assert os.environ["DISPLAY"] == ":0"
        assert os.environ["XAUTHORITY"] == "/host/xauth"


def test_an_existing_xauthority_is_never_overridden(tmp_path, monkeypatch):
    host_xauthority = tmp_path / "host-xauth"
    game_xauthority = tmp_path / "game-xauth"
    host_xauthority.write_text("")
    game_xauthority.write_text("")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("XAUTHORITY", str(host_xauthority))
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": str(game_xauthority)},
    )

    with launching._game_display([7]) as borrowed:
        assert borrowed is True
        assert os.environ["DISPLAY"] == ":7"
        assert os.environ["XAUTHORITY"] == str(host_xauthority)

    assert os.environ.get("DISPLAY") is None
    assert os.environ["XAUTHORITY"] == str(host_xauthority)


def test_a_pid_without_a_display_is_skipped_for_a_later_one(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda pid: {} if pid == 7 else {"DISPLAY": ":9"},
    )

    with launching._game_display([7, 8]) as borrowed:
        assert borrowed is True
        assert os.environ.get("DISPLAY") == ":9"

    assert os.environ.get("DISPLAY") is None


def test_a_container_only_xauthority_is_not_borrowed(tmp_path, monkeypatch):
    """A path only the game's container sees makes libXau fail; the default must apply."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(
        launching,
        "environ_of",
        lambda _pid: {
            "DISPLAY": ":7",
            "XAUTHORITY": str(tmp_path / "xauth-in-the-container"),
        },
    )

    with launching._game_display([7]) as borrowed:
        assert borrowed is True
        assert os.environ.get("DISPLAY") == ":7"
        assert "XAUTHORITY" not in os.environ, "a container path would block libXau's default"


def test_a_later_pid_can_supply_the_host_xauthority(tmp_path, monkeypatch):
    xauthority = tmp_path / "xauth"
    xauthority.write_text("")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    environments = {
        7: {"DISPLAY": ":7", "XAUTHORITY": str(tmp_path / "container-xauth")},
        8: {"DISPLAY": ":7", "XAUTHORITY": str(xauthority)},
    }
    monkeypatch.setattr(launching, "environ_of", lambda pid: environments[pid])

    with launching._game_display([7, 8]) as borrowed:
        assert borrowed is True
        assert os.environ.get("DISPLAY") == ":7"
        assert os.environ.get("XAUTHORITY") == str(xauthority)

    assert os.environ.get("DISPLAY") is None
    assert os.environ.get("XAUTHORITY") is None


def test_the_display_borrow_looks_environ_up_where_it_is_used(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(launching.wine, "environ_of", lambda _pid: {"DISPLAY": ":5"})

    with launching._game_display([7]) as borrowed:
        assert borrowed is True
        assert os.environ.get("DISPLAY") == ":5"

    assert os.environ.get("DISPLAY") is None


# ---- joining a real Sandbox --------------------------------------------------------------


def _bwrap_or_skip():
    bwrap = shutil.which("bwrap")
    if bwrap is None or shutil.which("nsenter") is None:
        pytest.skip("bubblewrap or nsenter is absent")
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "--", "/bin/true"],
        capture_output=True,
    )
    if probe.returncode:
        pytest.skip("bubblewrap cannot make namespaces here")
    return bwrap


def _children(pid):
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    return [int(p) for p in text.split()]


def _innermost(pid, timeout=5.0):
    """The deepest descendant of ``pid`` (the stand-in game), once it exists."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        node = pid
        while kids := _children(node):
            node = kids[0]
        if node != pid and Path(f"/proc/{node}/cmdline").read_bytes().startswith(b"sleep"):
            return node
        time.sleep(0.05)
    raise AssertionError("the stand-in game never started")


@pytest.mark.parametrize("own_pids", [True, False], ids=["own-pid-namespace", "shared-pids"])
def test_a_real_sandbox_is_joined_and_sees_its_own_tmp(own_pids):
    bwrap = _bwrap_or_skip()
    flags = [bwrap, "--die-with-parent", "--dev-bind", "/", "/", "--tmpfs", "/tmp"]
    if own_pids:
        flags += ["--unshare-pid", "--proc", "/proc"]
    # The game itself runs one level down, as umu and pressure-vessel put it.
    script = "echo inside > /tmp/marker; sh -c 'exec sleep 30' & wait"
    box = subprocess.Popen([*flags, "--", "sh", "-c", script])
    try:
        game = _innermost(box.pid)
        enter = sandboxjoin.enter_argv([game])
        assert enter is not None
        assert enter[0].endswith("nsenter") and enter[-1] == "--"
        assert ("-p" in enter) is own_pids, "only a namespace that differs is joined"
        out = subprocess.run(
            [*enter, "sh", "-c", "cat /tmp/marker; echo $$"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 0, out.stderr
        marker, pid = out.stdout.split()
        assert marker == "inside", "the Sandbox's private /tmp, where its wineserver lives"
        if own_pids:
            assert int(pid) < 100, "the helper runs in the game's PID namespace"
        assert not Path("/tmp/marker").exists() or Path("/tmp/marker").read_text() != "inside\n"
    finally:
        box.send_signal(signal.SIGTERM)
        box.wait(timeout=10)


def test_a_process_outside_any_sandbox_is_not_joined():
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert sandboxjoin.sandbox_root(child.pid) is None
        assert sandboxjoin.enter_argv([child.pid]) is None
    finally:
        child.kill()
        child.wait()


def test_a_namespace_that_is_not_a_bwrap_sandbox_is_not_joined():
    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare is absent")
    child = subprocess.Popen([unshare, "-Urm", "sleep", "30"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                theirs = os.readlink(f"/proc/{child.pid}/ns/mnt")
            except OSError:
                break  # unshare exited: no namespaces allowed here (CI containers)
            if theirs != os.readlink("/proc/self/ns/mnt"):
                break
            time.sleep(0.05)
        else:
            pytest.skip("unshare could not make a mount namespace here")
        if child.poll() is not None:
            pytest.skip("unshare cannot make namespaces here")
        assert sandboxjoin.sandbox_root(child.pid) is None
    finally:
        child.kill()
        child.wait()


def test_a_gone_process_is_not_joined():
    assert sandboxjoin.enter_argv([2**22 + 12345]) is None
    assert sandboxjoin.enter_argv([]) is None


def test_without_nsenter_nothing_is_joined(monkeypatch):
    monkeypatch.setattr(sandboxjoin.shutil, "which", lambda name, *_a, **_k: None)
    assert sandboxjoin.enter_argv([os.getpid()]) is None


#: The host's own namespaces, as the fake /proc spells them for Nadare.
_HOST_NS = {"mnt": "host", "user": "host:user", "pid": "host:pid"}


def _fake_proc_tree(tmp_path, monitor_name, *, share_pid=False):
    """A fake /proc: a game inside a bwrap monitor, whose host pids have host namespaces."""

    def proc(pid, comm, parent, namespaces):
        entry = tmp_path / str(pid)
        (entry / "ns").mkdir(parents=True)
        (entry / "stat").write_text(f"{pid} ({comm}) S {parent} 0 0")
        (entry / "comm").write_text(comm + "\n")
        for name, target in namespaces.items():
            (entry / "ns" / name).symlink_to(target)

    for name, target in _HOST_NS.items():
        entry = tmp_path / "self" / "ns"
        entry.mkdir(parents=True, exist_ok=True)
        (entry / name).symlink_to(target)
    sandbox = {
        "mnt": "sandbox",
        "user": "sandbox:user",
        "pid": _HOST_NS["pid"] if share_pid else "sandbox:pid",
    }
    # The game, then bwrap's child (inside), its monitor (still in Nadare's namespaces),
    # and Nadare itself. Only the sandbox processes differ from the host's namespaces.
    proc(31, "QRSL.exe", 30, sandbox)
    proc(30, "sh", 20, sandbox)
    proc(20, monitor_name, 10, _HOST_NS)
    proc(10, "nadare", 1, _HOST_NS)


@pytest.mark.parametrize(
    ("bwrap_name", "share_pid"),
    [("bwrap", False), ("srt-bwrap", False), ("bwrap", True)],
    ids=["bwrap", "srt-bwrap", "shared-pids"],
)
def test_the_sandbox_root_is_recognised_by_bubblewraps_own_name(
    tmp_path, bwrap_name, share_pid, monkeypatch
):
    """A fake /proc: the walk must stop at the bwrap monitor whatever the binary is called.

    The Sandbox runs the runtime's copy, ``srt-bwrap`` (``gamesandbox.locate`` prefers it),
    not the system ``bwrap``; checking only the literal ``bwrap`` made such a Sandbox fall
    back to its windows every time.
    """
    monkeypatch.setattr(sandboxjoin.shutil, "which", lambda name, *_a, **_k: "/usr/bin/nsenter")
    _fake_proc_tree(tmp_path, bwrap_name, share_pid=share_pid)

    assert sandboxjoin.sandbox_root(31, proc_root=tmp_path) == 30
    enter = sandboxjoin.enter_argv([31], proc_root=tmp_path)
    flags = ["-U", "--preserve-credentials", "-m"] + ([] if share_pid else ["-p"])
    assert enter is not None
    assert enter[1:] == ["-t", "30", *flags, "--"]
    assert ("-p" in enter) is not share_pid, "only a namespace that differs is joined"


@pytest.mark.parametrize("monitor_name", ["not-bwrap", "unshare"])
def test_a_monitor_that_only_ends_in_bwrap_is_not_joined(
    tmp_path, monitor_name, monkeypatch
):
    """``not-bwrap`` is not bubblewrap; the exact names are ``bwrap`` and ``srt-bwrap``."""
    monkeypatch.setattr(sandboxjoin.shutil, "which", lambda name, *_a, **_k: "/usr/bin/nsenter")
    _fake_proc_tree(tmp_path, monitor_name)

    assert sandboxjoin.sandbox_root(31, proc_root=tmp_path) is None
    assert sandboxjoin.enter_argv([31], proc_root=tmp_path) is None


def test_a_sandbox_started_by_the_runtimes_bwrap_is_joined(tmp_path):
    """The real binary under the name the Steam Runtime uses (``srt-bwrap``)."""
    bwrap = _bwrap_or_skip()
    runtime_bwrap = tmp_path / "srt-bwrap"
    shutil.copy2(bwrap, runtime_bwrap)
    box = subprocess.Popen(
        [
            str(runtime_bwrap),
            "--die-with-parent",
            "--dev-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--",
            "sh",
            "-c",
            "exec sleep 30",
        ]
    )
    try:
        # A copied binary can be refused (AppArmor profiles the real path): wait for the
        # child namespace, then skip rather than fail the suite for this host's policy.
        game = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if box.poll() is not None:
                pytest.skip("the copied srt-bwrap cannot start here")
            try:
                game = _innermost(box.pid, timeout=0.2)
            except AssertionError:
                continue
            break
        if game is None or box.poll() is not None:
            pytest.skip("the copied srt-bwrap cannot start here")
        root = sandboxjoin.sandbox_root(game)
        assert root is not None, "the runtime's bwrap is not recognised"
        assert Path(f"/proc/{root}/comm").read_text().strip() == "srt-bwrap"
        enter = sandboxjoin.enter_argv([game])
        assert enter is not None
        done = subprocess.run([*enter, "true"], timeout=10, check=False)
        assert done.returncode == 0
    finally:
        box.send_signal(signal.SIGTERM)
        box.wait(timeout=10)
