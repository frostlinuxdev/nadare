# SPDX-License-Identifier: GPL-3.0-or-later
"""Stop asks a Sandboxed game to close through the X server's own idea of who owns a window.

A Sandboxed game has its own PID namespace (#1593), so Wine writes a namespace-local pid into
``_NET_WM_PID``; two Sandboxed games can write the same one, so Stop refused those windows and
ended the game at once ("no X11 window was found"). The X-Resource extension reports the pid
of the client that made each window as the X server sees it: a host pid, unique, the one
``scan_pids`` returns. Without the extension Stop behaves as before.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest
import test_stop_grace_1421 as base

from nadare.core import x11windows
from nadare.core.launch_window import WindowEvidence


def _run_stop(tmp_path, monkeypatch, windows, clients):
    backend, events = base._backend(tmp_path)
    monkeypatch.setattr(backend, "secure_mode_enabled", lambda: True)
    monkeypatch.setenv("DISPLAY", ":1")
    clock = [100.0]
    base._fake_clock(monkeypatch, clock)
    alive = {7}
    calls = []
    base._patch_stop(monkeypatch, alive, calls)
    base._seed_polite_stop(backend, kind="x11")
    sent = []
    base._fake_x11(monkeypatch, windows, sent)
    monkeypatch.setattr(x11windows, "client_pids", lambda ids: clients)
    backend.stop()
    logged = [e.message for e in events if e.kind == "log"]
    return sent, calls, clock[0], logged


def test_a_window_with_a_namespace_pid_is_closed_when_the_server_names_the_game(
    tmp_path, monkeypatch
):
    # Window 11 says pid 3 (inside the Sandbox); the X server says its client is host pid 7.
    sent, calls, now, _ = _run_stop(tmp_path, monkeypatch, [(11, 3), (12, 99)], {11: 7, 12: 555})
    assert sent == [[11]]
    assert calls == [("term", [7])]
    assert now == 108.0, "the grace runs after the close request"


def test_another_programs_window_is_never_closed_even_with_a_matching_local_pid(
    tmp_path, monkeypatch
):
    # Both windows say pid 7, but the server says window 12 belongs to someone else.
    sent, _, _, _ = _run_stop(tmp_path, monkeypatch, [(11, 7), (12, 7)], {11: 7, 12: 555})
    assert sent == [[11]]


def test_without_the_extension_stop_is_as_before(tmp_path, monkeypatch):
    sent, calls, now, logged = _run_stop(tmp_path, monkeypatch, [(11, 3)], None)
    assert sent == []
    assert calls == [("term", [7])]
    assert now == 100.0
    assert any("no X11 window" in m for m in logged), logged


def test_without_the_extension_host_tree_pids_still_count(tmp_path, monkeypatch):
    sent, _, _, _ = _run_stop(tmp_path, monkeypatch, [(11, 7)], None)
    assert sent == [[11]]


def test_owned_by_client_takes_the_tree_and_ignores_namespace_exclusion(tmp_path):
    def proc(pid, parent=1, nspid=None):
        entry = tmp_path / str(pid)
        entry.mkdir(parents=True)
        (entry / "stat").write_text(f"{pid} (game) S {parent} 0 0")
        if nspid is not None:
            (entry / "status").write_text(f"Name:\tgame.exe\nNSpid:\t{nspid}\n")

    proc(10, nspid="10 2")
    proc(11, parent=10, nspid="11 11")  # an inner pid equal to a host pid: still ours
    probe = WindowEvidence(set(), proc_root=tmp_path)
    assert probe.owned_by_client([10], {1: 10, 2: 11, 3: 99}) == {1: 10, 2: 11}


# ---- client_pids itself -------------------------------------------------------------------


def test_client_pids_without_a_display_is_unknown(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert x11windows.client_pids([5]) is None


def test_client_pids_without_libxres_is_unknown(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setattr(x11windows, "_load_xres", lambda: None)
    assert x11windows.client_pids([5]) is None


def test_client_pids_of_nothing_is_empty():
    assert x11windows.client_pids([]) == {}


def _free_display():
    for n in range(91, 140):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    pytest.skip("no free X display number")


_CHILD = r"""
import ctypes, ctypes.util, sys, time
x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
x.XOpenDisplay.restype = ctypes.c_void_p
x.XOpenDisplay.argtypes = [ctypes.c_char_p]
x.XDefaultRootWindow.restype = ctypes.c_ulong
x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x.XCreateSimpleWindow.restype = ctypes.c_ulong
x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_int] * 2 + [
    ctypes.c_uint] * 3 + [ctypes.c_ulong] * 2
x.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x.XFlush.argtypes = [ctypes.c_void_p]
x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
d = x.XOpenDisplay(None)
w = x.XCreateSimpleWindow(d, x.XDefaultRootWindow(d), 0, 0, 10, 10, 0, 0, 0)
x.XMapWindow(d, w)
x.XSync(d, 0)
print(w, __import__("os").getpid(), flush=True)
time.sleep(30)
"""


def test_client_pids_on_a_real_x_server_names_the_creating_process(tmp_path):
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb is absent")
    if x11windows._load_xres() is None:
        pytest.skip("libXRes is absent")
    n = _free_display()
    server = subprocess.Popen(
        ["Xvfb", f":{n}", "-nolisten", "tcp", "-screen", "0", "64x64x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        for _ in range(100):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                break
            time.sleep(0.05)
        env = dict(os.environ, DISPLAY=f":{n}")
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD], env=env, stdout=subprocess.PIPE, text=True
        )
        window, _inner = map(int, child.stdout.readline().split())
        old = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = f":{n}"
        try:
            got = x11windows.client_pids([window, 0x7FFFFF])
        finally:
            if old is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = old
        assert got is not None
        assert got.get(window) == child.pid
        assert 0x7FFFFF not in got, "an unknown window has no owner"
    finally:
        if child is not None:
            child.kill()
            child.wait()
        server.kill()
        server.wait()


def _descendants(pid):
    out, todo = set(), [pid]
    while todo:
        parent = todo.pop()
        try:
            kids = pathlib.Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        except OSError:
            continue
        for kid in map(int, kids):
            out.add(kid)
            todo.append(kid)
    return out


def test_a_window_made_inside_a_pid_namespace_is_named_by_its_host_pid():
    """The whole point: _NET_WM_PID would say the inner pid; the X server says the host one."""
    bwrap = shutil.which("bwrap")
    if shutil.which("Xvfb") is None or bwrap is None:
        pytest.skip("Xvfb or bubblewrap is absent")
    if x11windows._load_xres() is None:
        pytest.skip("libXRes is absent")
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "--", "/bin/true"],
        capture_output=True,
    )
    if probe.returncode:
        pytest.skip("bubblewrap cannot make a PID namespace here")
    n = _free_display()
    server = subprocess.Popen(
        ["Xvfb", f":{n}", "-nolisten", "tcp", "-screen", "0", "64x64x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        for _ in range(100):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                break
            time.sleep(0.05)
        env = dict(os.environ, DISPLAY=f":{n}")
        argv = [bwrap, "--dev-bind", "/", "/", "--unshare-pid", "--proc", "/proc", "--"]
        child = subprocess.Popen(
            argv + [sys.executable, "-c", _CHILD], env=env, stdout=subprocess.PIPE, text=True
        )
        window, inner = map(int, child.stdout.readline().split())
        old = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = f":{n}"
        try:
            got = x11windows.client_pids([window])
        finally:
            if old is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = old
        assert got is not None and window in got
        host = got[window]
        assert host != inner, "the namespace pid would be ambiguous"
        assert host in _descendants(child.pid), "the X server names the real (host) process"
    finally:
        if child is not None:
            child.kill()
            child.wait()
        server.kill()
        server.wait()
