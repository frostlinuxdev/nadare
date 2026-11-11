# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE "Log in" without the launcher: the INTL service drives the Google sign-in itself.

Measured on 2026-11-10 (agent notes, TBS-BROWSER-2026-11-10.md "Item 8 resolved"): sending the
service ``LOGIN_REQ`` with channel 6 and ``auto_login=0`` makes ``intl_service.exe`` open the Google
page in the system browser, listen on its own port, exchange the code and write ``intl_auth.txt``.
Running our own ``/jssdk`` listener and handing it the code (``oauth.py``) does not work and
is gone.

Nothing here runs Wine: the runner command and the output stream are replaced.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendError, get, supports_login

ROOT = Path(__file__).resolve().parents[1]

AUTH = {
    "openid": "10000000000000000001",
    "token": "0123456789abcdef0123456789abcdef01234567",
    "pf": "LevelInfinite_Example",
    "pf_key": "00000000000000000000000000000000",
    "bind_list": "[]",
}


def make_install(tmp_path, *, signed_in: bool = False) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    launcher = write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    write_exe(prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe", "MZ")
    (prefix / "drive_c" / "NIKKE" / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    if signed_in:
        storage = h.cache_storage_dir(prefix)
        write_exe(storage / "intl_auth.txt", "session")
        write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def _quiet(backend: NikkeBackend, monkeypatch) -> list:
    monkeypatch.setattr(
        NikkeBackend, "_prefix_command", lambda self, args, extra_env=None, **kw: (args, {})
    )
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    notes: list = []
    backend.info = notes.append
    return notes


def _service_signs_in(backend: NikkeBackend, calls: list, *, write_cache: bool = True):
    """A fake runner: the INTL host writes the session file, the service writes its cache."""

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        calls.append(list(argv))
        out = argv[argv.index("--out") + 1]
        relative = out.split(":\\", 1)[1].replace("\\", "/")
        session_file = backend.prefix() / "drive_c" / relative
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        if write_cache:
            storage = h.cache_storage_dir(backend.prefix())
            write_exe(storage / "intl_auth.txt", "new session")
            write_exe(storage / "mmkv" / "mmkv.default", "device")
        return 0

    return fake_stream


def _arg(argv: list, name: str) -> str:
    return argv[argv.index(name) + 1]


# ---- the contract ---------------------------------------------------------------------------
def test_nikke_has_a_login_step():
    assert get("launcher-nikke").supports_login is True
    assert supports_login("launcher-nikke") is True


def test_intl_command_can_wait_minutes_for_a_human():
    cmd = h.intl_command("host.exe", "svc.exe", "C:\\out.json", auto_login=0, rsp_timeout_ms=600000)
    assert _arg(cmd, "--auto-login") == "0"
    assert _arg(cmd, "--rsp-timeout-ms") == "600000"


def test_intl_command_keeps_the_host_default_wait_when_not_asked():
    assert "--rsp-timeout-ms" not in h.intl_command("host.exe", "svc.exe", "C:\\out.json")


# ---- Log in -------------------------------------------------------------------------------
def test_login_lets_the_service_run_the_google_sign_in(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _quiet(backend, monkeypatch)
    calls: list = []
    monkeypatch.setattr(launcher_module, "stream", _service_signs_in(backend, calls))
    events = []
    backend.ctx.emit = events.append

    backend.login()

    assert len(calls) == 1
    argv = calls[0]
    assert h.INTL_HOST_NAME in argv[0]
    assert _arg(argv, "--auto-login") == "0"  # a fresh sign-in, not the saved session
    assert _arg(argv, "--login-channel") == "6"  # Google
    assert int(_arg(argv, "--rsp-timeout-ms")) >= 300000  # time for a human in the browser
    # the session the service saved is now Nadare's own copy
    assert h.has_cache(backend._session_cache())
    assert (backend._session_cache() / "intl_auth.txt").read_text() == "new session"
    assert any(getattr(e, "kind", "") == "done" for e in events)


def test_login_tells_the_user_where_to_look(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _quiet(backend, monkeypatch)
    monkeypatch.setattr(launcher_module, "stream", _service_signs_in(backend, []))
    events = []
    backend.ctx.emit = events.append

    backend.login()

    steps = " ".join(e.message for e in events if getattr(e, "kind", "") == "step")
    assert "browser" in steps.lower()


def test_a_failed_login_raises_and_keeps_no_cache(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _quiet(backend, monkeypatch)
    monkeypatch.setattr(launcher_module, "stream", lambda *a, **k: 3)

    with pytest.raises(BackendError, match="(?i)sign-in"):
        backend.login()
    assert not h.has_cache(backend._session_cache())


def test_a_login_without_a_saved_service_cache_fails(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _quiet(backend, monkeypatch)
    monkeypatch.setattr(
        launcher_module, "stream", _service_signs_in(backend, [], write_cache=False)
    )

    with pytest.raises(BackendError):
        backend.login()
    assert not h.has_cache(backend._session_cache())


def test_login_needs_the_launcher_installed_first(tmp_path, monkeypatch):
    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "empty")})
    )
    monkeypatch.setattr(launcher_module, "stream", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(BackendError, match="(?i)install"):
        backend.login()


def test_login_does_not_reuse_an_old_service_cache(tmp_path, monkeypatch):
    """A stale intl_auth.txt in the prefix must not make a failed sign-in look like success."""
    backend = make_install(tmp_path, signed_in=True)
    _quiet(backend, monkeypatch)
    monkeypatch.setattr(
        launcher_module, "stream", _service_signs_in(backend, [], write_cache=False)
    )
    with pytest.raises(BackendError):
        backend.login()
    assert not h.has_cache(backend._session_cache())


# ---- status and Log out ----------------------------------------------------------------------
def test_status_says_whether_a_session_is_saved(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    assert backend.status().data.get("logged_in") is False
    h.save_cache(_storage_with_session(backend), backend._session_cache())
    assert backend.status().data.get("logged_in") is True


def _storage_with_session(backend: NikkeBackend) -> Path:
    storage = h.cache_storage_dir(backend.prefix())
    write_exe(storage / "intl_auth.txt", "session")
    return storage


def test_logout_forgets_both_copies_of_the_session(tmp_path):
    backend = make_install(tmp_path, signed_in=True)
    h.save_cache(h.cache_storage_dir(backend.prefix()), backend._session_cache())
    events = []
    backend.ctx.emit = events.append

    backend.logout()

    assert not h.has_cache(backend._session_cache())
    assert not (h.cache_storage_dir(backend.prefix()) / "intl_auth.txt").exists()
    assert any(getattr(e, "kind", "") == "done" for e in events)


# ---- the broken path is gone -------------------------------------------------------------------
def test_the_own_listener_google_path_is_removed():
    assert importlib.util.find_spec("nadare.backends.nikke.oauth") is None
    script = (ROOT / "scripts" / "nikke_launcherless" / "launcherless.py").read_text()
    assert "--login-google" not in script
    assert not (ROOT / "tests" / "test_nikke_oauth.py").exists()


@pytest.mark.parametrize(
    "source",
    [
        "src/nadare/backends/nikke/hosts_src/intlsvc_host.c",
        "scripts/nikke_launcherless/intlsvc_host.c",
    ],
)
def test_the_host_never_prints_reply_bytes(source):
    """The reply starts with the account id: printing its first bytes leaked part of it."""
    text = (ROOT / source).read_text()
    assert "head:" not in text


# ---- the Sandbox ------------------------------------------------------------------------------
def _recording_wrap(monkeypatch) -> list:
    """Replace the Sandbox wrapper with one that marks argv and records the call."""
    wraps: list = []

    def wrap(self, argv, env, *, subject="game", read_only=(), links=True):
        wraps.append({"subject": subject, "links": links, "argv0": argv[0]})
        return ["SANDBOX", *argv], env

    monkeypatch.setattr(NikkeBackend, "_secure_wrap", wrap)
    return wraps


def test_login_runs_inside_the_sandbox_and_can_open_the_browser(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _quiet(backend, monkeypatch)
    wraps = _recording_wrap(monkeypatch)
    calls: list = []
    monkeypatch.setattr(launcher_module, "stream", _service_signs_in(backend, calls))
    backend.ctx.emit = lambda _e: None

    backend.login()

    assert calls[0][0] == "SANDBOX"  # what ran is the wrapped command
    assert len(wraps) == 1
    assert wraps[0]["links"] is True  # the service opens Google through the link relay


def test_a_launcherless_launch_runs_both_hosts_inside_the_sandbox(tmp_path, monkeypatch):
    import contextlib

    backend = make_install(tmp_path, signed_in=True)
    backend.ctx.profile["launcherless"] = True
    _quiet(backend, monkeypatch)
    wraps = _recording_wrap(monkeypatch)
    calls: list = []

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        calls.append(list(argv))
        if any(h.INTL_HOST_NAME in part for part in argv):
            out = argv[argv.index("--out") + 1]
            relative = out.split(":\\", 1)[1].replace("\\", "/")
            session_file = backend.prefix() / "drive_c" / relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        return 0

    class _NoProbe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(launcher_module, "stream", fake_stream)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(NikkeBackend, "_renderer_probe", lambda self: _NoProbe())
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )

    assert backend.launch() == 0
    assert len(calls) == 2
    assert all(argv[0] == "SANDBOX" for argv in calls)  # sign-in host and Sail host (the game)
    assert len(wraps) == 2


# ---- no console window --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [h.INTL_HOST_NAME, h.SAIL_HOST_NAME])
def test_the_helpers_are_windowless_programs(name):
    """Proton opens a console window for a console-subsystem .exe; the helpers must not show one."""
    data = (ROOT / "src" / "nadare" / "assets" / name).read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    assert data[pe:pe + 4] == b"PE\0\0"
    subsystem = int.from_bytes(data[pe + 0x5C:pe + 0x5E], "little")
    assert subsystem == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI (3 would be a console program)


def test_the_build_script_builds_windowless_programs():
    script = (ROOT / "src" / "nadare" / "backends" / "nikke" / "build_hosts.sh").read_text()
    assert "-mwindows" in script


def test_a_successful_play_keeps_the_sail_helper_log(tmp_path, monkeypatch):
    """The helpers are windowless, so their output reaches Nadare only through the log file.
    Which Sail commands the game sends during play (101, 102, ...) must stay visible."""
    import contextlib

    backend = make_install(tmp_path, signed_in=True)
    backend.ctx.profile["launcherless"] = True
    notes = _quiet(backend, monkeypatch)
    monkeypatch.setattr(NikkeBackend, "_secure_wrap", lambda self, argv, env, **kw: (argv, env))

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        if any(h.INTL_HOST_NAME in part for part in argv):
            out = argv[argv.index("--out") + 1]
            relative = out.split(":\\", 1)[1].replace("\\", "/")
            session_file = backend.prefix() / "drive_c" / relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        else:  # the Sail host writes its log next to the session
            stage = backend.prefix().joinpath(*h.STAGE_PARTS)
            (work,) = list(stage.glob("session-*"))
            (work / "sail_host.log").write_text(
                "*** game accepted init (cmd=101)\n<< frame (7 proto bytes) cmd=102\n"
                "<< frame (9 proto bytes) cmd=106\n",
                encoding="utf-8",
            )
        return 0

    class _NoProbe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(launcher_module, "stream", fake_stream)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(NikkeBackend, "_renderer_probe", lambda self: _NoProbe())
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )

    assert backend.launch() == 0
    sail = [n for n in notes if n.startswith("sail: ")]
    assert "sail: << frame (9 proto bytes) cmd=106" in sail
    assert "sail: *** game accepted init (cmd=101)" in sail
