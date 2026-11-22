# SPDX-License-Identifier: GPL-3.0-or-later
"""One runner hosts both the saved sign-in and the game; no Wine required."""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path

import pytest
from test_nikke_game_auth_cache_1420 import (
    AUTH,
    REJECTED,
    Runner,
    _unix,
    make_install,
)
from test_nikke_game_auth_cache_1420 import play as play

from nadare.backends import launcher as launcher_module
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled


@pytest.fixture(autouse=True)
def bounded_wait():
    def expired(_signum, _frame):
        raise AssertionError("launch/thread cleanup exceeded 10 seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        # Only this feature's thread: other tests in a full run leave their own behind.
        assert not [t for t in threading.enumerate() if t.name == "nikke-session"]


def assert_clean(backend):
    stage = backend.prefix().joinpath(*h.STAGE_PARTS)
    assert list(stage.glob("session-*")) == []


def test_sign_in_and_game_share_one_runner_and_fresh_session(tmp_path, play):
    backend = make_install(tmp_path)
    fresh = {**AUTH, "token": "fresh-token"}
    runner = Runner(backend, auth=fresh)
    notes = []
    backend.ctx.emit = lambda event: notes.append(event.message)
    assert play(backend, runner, notes=notes) == 0
    assert runner.names == ["sail"]
    command = runner.envs[0]["SAIL_SIGNIN_CMD"]
    assert command.startswith('C:\\') or command.startswith('"C:\\')
    assert h.INTL_HOST_NAME in command
    assert "--out" in command and "session.enc.json" in command
    assert runner.sessions[0]["token"] == fresh["token"]
    assert runner.session_modes == [0o600]
    assert h.load_game_auth(backend._session_cache(), h.auth_fingerprint(
        h.cache_storage_dir(backend.prefix())
    )) == fresh
    assert any("using the saved session, no launcher" in note for note in notes)
    assert any("launching the game" in note for note in notes)
    assert not any(fresh["token"] in note for note in notes)
    assert_clean(backend)


def test_refused_kept_blob_uses_one_combined_retry(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    fresh = {**AUTH, "token": "replacement-token"}
    runner = Runner(backend, sail_logs=[REJECTED, "RESULT ok"], auth=fresh)
    play(backend, runner)
    assert runner.names == ["sail", "sail"]
    assert "SAIL_SIGNIN_CMD" not in runner.envs[0]
    assert "SAIL_SIGNIN_CMD" in runner.envs[1]
    assert runner.sessions[1]["token"] == fresh["token"]
    assert (backend._session_cache() / h.GAME_AUTH_NAME).exists()
    assert_clean(backend)


@pytest.mark.parametrize("auth", [{"openid": "1"}, [], "invalid JSON", ""])
def test_unusable_auth_times_out_without_publishing_a_session(tmp_path, play, auth):
    backend = make_install(tmp_path)

    def runner(argv, env, **kwargs):
        assert "SAIL_SIGNIN_CMD" in env
        session = _unix(backend, argv[1])
        enc = session.parent / "session.enc.json"
        enc.write_text(auth if isinstance(auth, str) else json.dumps(auth))
        time.sleep(0.15)
        assert not session.exists()
        (session.parent / "sail_host.log").write_text(
            "RESULT no-session: session file never appeared\n"
        )
        return 6

    with pytest.raises(BackendError, match="the NIKKE sign-in returned no session\\."):
        play(backend, runner)
    assert_clean(backend)


def test_helper_failure_reports_its_exit_code(tmp_path, play):
    backend = make_install(tmp_path)

    def runner(argv, env, **kwargs):
        assert "SAIL_SIGNIN_CMD" in env
        session = _unix(backend, argv[1])
        assert not session.exists()
        (session.parent / "sail_host.log").write_text(
            "12:34:00.001 RESULT no-signin: sign-in helper exit 23\n"
        )
        return 5

    with pytest.raises(BackendError, match="sign-in helper exit code 23"):
        play(backend, runner)
    assert_clean(backend)


@pytest.mark.parametrize("raise_cancel", [False, True])
def test_cancel_joins_the_session_waiter(tmp_path, play, raise_cancel):
    backend = make_install(tmp_path)
    before = set(threading.enumerate())

    def runner(argv, env, **kwargs):
        assert "SAIL_SIGNIN_CMD" in env
        backend.ctx.cancel.cancel()
        if raise_cancel:
            raise Cancelled()
        return 0

    try:
        play(backend, runner)
    except Cancelled:
        pass
    assert set(threading.enumerate()) == before
    assert_clean(backend)


def test_runner_exception_joins_the_session_waiter(tmp_path, play):
    backend = make_install(tmp_path)
    before = set(threading.enumerate())

    def runner(argv, env, **kwargs):
        assert "SAIL_SIGNIN_CMD" in env
        raise RuntimeError("runner failed")

    with pytest.raises(RuntimeError, match="runner failed"):
        play(backend, runner)
    assert set(threading.enumerate()) == before
    assert_clean(backend)


def test_session_is_published_only_after_the_write_finishes(tmp_path, play, monkeypatch):
    backend = make_install(tmp_path)
    original = h.make_session_file
    writes = []

    def writing(auth, path, **kwargs):
        path = Path(path)
        writes.append(path)
        path.write_text("{")
        assert not (path.parent / "session.json").exists()
        return original(auth, path, **kwargs)

    monkeypatch.setattr(launcher_module.nikke_sail, "make_session_file", writing)
    play(backend, Runner(backend))
    assert len(writes) == 1
    assert_clean(backend)


@pytest.mark.parametrize(("args", "expected"), [
    ([r"C:\Program Files\host.exe", "a b"], '"C:\\Program Files\\host.exe" "a b"'),
    (["host.exe", 'a"b', ""], 'host.exe a\\"b ""'),
    (["host.exe", "a b\\"], 'host.exe "a b\\\\"'),
    (["host.exe", 'a\\"b'], 'host.exe a\\\\\\"b'),
])
def test_windows_command_line_quotes_arguments(args, expected):
    assert h.windows_command_line(args) == expected


def test_sail_source_supports_optional_sign_in_and_session_wait():
    source = (Path(h.__file__).parent / "hosts_src" / "sail_host.c").read_text()
    main = source.split("int main(", 1)[1]
    assert 'GetEnvironmentVariableW(L"SAIL_SIGNIN_CMD"' in main
    assert 'getenv("SAIL_SESSION_WAIT_MS")' in main
    assert "RESULT no-signin: sign-in helper exit %" in main
    assert "RESULT no-session: session file never appeared" in main
    assert "CREATE_NO_WINDOW" in main
    assert "WaitForSingleObject" in main
