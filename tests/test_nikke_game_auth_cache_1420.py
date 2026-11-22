# SPDX-License-Identifier: GPL-3.0-or-later
"""#1420: a launcherless Play reuses the game-auth blob instead of starting the sign-in service.

Before, every Play started a whole runner (umu, Proton, Wine) only to run the INTL service and
get the same ~8 KB blob back: about 7 s of a ~21 s start. The blob carries its own expiry, so it
is kept in the profile's private sign-in folder and used while:

* it belongs to the saved sign-in it came from (a new sign-in makes it stale),
* it does not expire within a day,
* the game accepted it last time (a rejected init drops it and signs in again in the same Play).

Nothing here runs Wine: the runner command and the output stream are replaced.
"""

from __future__ import annotations

import contextlib
import json
import os
import time

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h

FAR = int(time.time()) + 20 * 86400

AUTH = {
    "openid": "10000000000000000001",
    "token": "0123456789abcdef0123456789abcdef01234567",
    "pf": "LevelInfinite_Example",
    "pf_key": "00000000000000000000000000000000",
    "bind_list": "[]",
    "token_expire_time": FAR,
}

ACCEPTED = "*** game accepted init (cmd=101)\nRESULT ok: game accepted the Sail init handshake\n"
REJECTED = (
    "game did not reply cmd=101 (got 0)\n"
    "RESULT no-init: game connected but never returned cmd=101\n"
)


# ------------------------------------------------------------------ the store itself


def test_a_saved_blob_comes_back_for_the_same_sign_in(tmp_path):
    assert h.save_game_auth(tmp_path, AUTH, "fp-1") is True
    assert h.load_game_auth(tmp_path, "fp-1") == AUTH


def test_a_blob_from_another_sign_in_is_not_used(tmp_path):
    h.save_game_auth(tmp_path, AUTH, "fp-1")
    assert h.load_game_auth(tmp_path, "fp-2") is None
    assert h.load_game_auth(tmp_path, None) is None


# Offsets, not timestamps: a time-based id differs between xdist workers that collect a second
# apart, and the whole run fails with "Different tests were collected".
@pytest.mark.parametrize("offset", [3600, -10, None], ids=["in-an-hour", "expired", "zero"])
def test_a_blob_that_expires_within_a_day_is_not_used(tmp_path, offset):
    expiry = 0 if offset is None else int(time.time()) + offset
    h.save_game_auth(tmp_path, {**AUTH, "token_expire_time": expiry}, "fp")
    assert h.load_game_auth(tmp_path, "fp") is None


@pytest.mark.parametrize("expiry", [None, "soon", "1e400", True, [1]])
def test_a_blob_without_a_usable_expiry_is_not_kept(tmp_path, expiry):
    auth = dict(AUTH)
    if expiry is None:
        del auth["token_expire_time"]
    else:
        auth["token_expire_time"] = expiry
    assert h.save_game_auth(tmp_path, auth, "fp") is False
    assert not (tmp_path / h.GAME_AUTH_NAME).exists()


def test_an_expiry_given_as_digits_is_accepted(tmp_path):
    assert h.save_game_auth(tmp_path, {**AUTH, "token_expire_time": str(FAR)}, "fp") is True
    assert h.load_game_auth(tmp_path, "fp") is not None


def test_a_blob_missing_a_required_field_is_not_kept(tmp_path):
    auth = {k: v for k, v in AUTH.items() if k != "pf_key"}
    assert h.save_game_auth(tmp_path, auth, "fp") is False


def test_a_tampered_blob_missing_a_field_is_not_used(tmp_path):
    h.save_game_auth(tmp_path, AUTH, "fp")
    path = tmp_path / h.GAME_AUTH_NAME
    doc = json.loads(path.read_text())
    del doc["auth"]["token"]
    path.write_text(json.dumps(doc))
    assert h.load_game_auth(tmp_path, "fp") is None


def test_the_blob_file_is_private(tmp_path):
    h.save_game_auth(tmp_path, AUTH, "fp")
    assert (tmp_path / h.GAME_AUTH_NAME).stat().st_mode & 0o777 == 0o600


def test_a_link_at_the_blob_path_is_replaced_not_followed(tmp_path):
    # The link points at a blob that would be accepted if it were read.
    planted = tmp_path / "planted"
    planted.mkdir()
    h.save_game_auth(planted, {**AUTH, "token": "planted"}, "fp")
    outside = planted / h.GAME_AUTH_NAME
    before = outside.read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / h.GAME_AUTH_NAME).symlink_to(outside)

    assert h.load_game_auth(cache, "fp") is None  # a link is never read
    h.save_game_auth(cache, AUTH, "fp")

    assert outside.read_bytes() == before
    assert not (cache / h.GAME_AUTH_NAME).is_symlink()


def test_garbage_in_the_blob_file_is_not_used(tmp_path):
    (tmp_path / h.GAME_AUTH_NAME).write_text("{not json")
    assert h.load_game_auth(tmp_path, "fp") is None
    (tmp_path / h.GAME_AUTH_NAME).write_text("[1, 2]")
    assert h.load_game_auth(tmp_path, "fp") is None


def test_drop_removes_the_blob_and_tolerates_none(tmp_path):
    h.save_game_auth(tmp_path, AUTH, "fp")
    h.drop_game_auth(tmp_path)
    assert not (tmp_path / h.GAME_AUTH_NAME).exists()
    h.drop_game_auth(tmp_path)  # nothing there: no error


def test_the_fingerprint_follows_the_saved_sign_in(tmp_path):
    write_exe(tmp_path / "intl_auth.txt", "session one")
    first = h.auth_fingerprint(tmp_path)
    (tmp_path / "intl_auth.txt").write_text("session two")
    assert first is not None
    assert h.auth_fingerprint(tmp_path) not in (None, first)


def test_no_fingerprint_without_a_sign_in_or_through_a_link(tmp_path):
    assert h.auth_fingerprint(tmp_path) is None
    real = tmp_path / "real.txt"
    real.write_text("session")
    (tmp_path / "intl_auth.txt").symlink_to(real)
    assert h.auth_fingerprint(tmp_path) is None


def test_the_blob_never_goes_into_the_prefix(tmp_path):
    cache = tmp_path / "cache"
    write_exe(cache / "intl_auth.txt", "session")
    write_exe(cache / "mmkv" / "mmkv.default", "device")
    h.save_game_auth(cache, AUTH, "fp")
    prefix_side = tmp_path / "prefix-storage"

    h.restore_cache(cache, prefix_side)

    assert not (prefix_side / h.GAME_AUTH_NAME).exists()
    assert (prefix_side / "intl_auth.txt").exists()


# ------------------------------------------------------------------- the launch


def make_install(tmp_path):
    prefix = tmp_path / "prefix"
    launcher = write_exe(
        prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ"
    )
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    write_exe(prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe", "MZ")
    (prefix / "drive_c" / "NIKKE" / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    storage = h.cache_storage_dir(prefix)
    write_exe(storage / "intl_auth.txt", "session")
    write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": True}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


class _NoProbe:
    result = None

    def start(self):
        return self

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


def _unix(backend, win: str):
    relative = win.split(":\\", 1)[1].replace("\\", "/")
    return backend.prefix() / "drive_c" / relative


class Runner:
    """Stands in for the runner: records each helper and answers like it would."""

    def __init__(self, backend, sail_logs=None, auth=None):
        self.backend = backend
        self.calls: list[list[str]] = []
        self.sessions: list[dict] = []
        self.envs: list[dict] = []
        self.session_modes: list[int] = []
        self.sail_logs = list(sail_logs or [ACCEPTED])
        self.auth = auth or AUTH

    def __call__(self, argv, env, on_line=None, cancel=None, **kwargs):
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        if h.INTL_HOST_NAME in argv[0]:
            out = _unix(self.backend, argv[argv.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(self.auth), encoding="utf-8")
        elif h.SAIL_HOST_NAME in argv[0]:
            session = _unix(self.backend, argv[1])
            if env.get("SAIL_SIGNIN_CMD"):
                (session.parent / "session.enc.json").write_text(json.dumps(self.auth))
                deadline = time.monotonic() + 2
                while not session.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert session.exists(), "Sail timed out waiting for Nadare's session"
            self.session_modes.append(session.stat().st_mode & 0o777)
            self.sessions.append(json.loads(session.read_text(encoding="utf-8")))
            log = self.sail_logs.pop(0) if len(self.sail_logs) > 1 else self.sail_logs[0]
            (session.parent / "sail_host.log").write_text(log, encoding="utf-8")
        return 0

    @property
    def names(self):
        return [
            "intl" if h.INTL_HOST_NAME in c[0] else "sail" if h.SAIL_HOST_NAME in c[0] else "?"
            for c in self.calls
        ]


@pytest.fixture
def play(monkeypatch):
    monkeypatch.setattr(
        NikkeBackend, "_prefix_command",
        lambda self, args, extra_env=None, **kw: (args, extra_env or {})
    )
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(NikkeBackend, "_renderer_probe", lambda self: _NoProbe())
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )
    # A real session lasts minutes; the fake one ends at once.
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 0)

    def run(backend, runner, notes=None):
        monkeypatch.setattr(launcher_module, "stream", runner)
        backend.info = notes.append if notes is not None else lambda _line: None
        return backend.launch()

    return run


def test_the_first_play_signs_in_and_keeps_the_blob(tmp_path, play):
    backend = make_install(tmp_path)
    runner = Runner(backend)

    assert play(backend, runner) == 0

    assert runner.names == ["sail"]
    assert (backend._session_cache() / h.GAME_AUTH_NAME).is_file()


def test_the_next_play_skips_the_sign_in_service(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    runner = Runner(backend)

    assert play(backend, runner) == 0

    assert runner.names == ["sail"]
    assert runner.sessions[0]["token"] == AUTH["token"]
    assert runner.sessions[0]["open_id"] == AUTH["openid"]
    stage = backend.prefix().joinpath(*h.STAGE_PARTS)
    assert list(stage.glob("session-*")) == []  # the run's session copy is still removed


def test_a_new_sign_in_in_the_launcher_makes_the_blob_stale(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    storage = h.cache_storage_dir(backend.prefix())
    (storage / "intl_auth.txt").write_text("a newer session")
    later = time.time() + 60
    os.utime(storage / "intl_auth.txt", (later, later))
    runner = Runner(backend)

    play(backend, runner)

    assert runner.names == ["sail"]


def test_a_rejected_init_drops_the_blob_and_signs_in_again_in_the_same_play(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    runner = Runner(backend, sail_logs=[REJECTED, ACCEPTED])

    assert play(backend, runner) == 0

    assert runner.names == ["sail", "sail"]
    # The fresh blob from the second sign-in is what is kept now.
    assert (backend._session_cache() / h.GAME_AUTH_NAME).is_file()


def test_a_rejected_init_after_a_fresh_sign_in_is_not_retried(tmp_path, play):
    backend = make_install(tmp_path)
    runner = Runner(backend, sail_logs=[REJECTED])

    play(backend, runner)

    assert runner.names == ["sail"]
    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()


def test_a_cancelled_play_does_not_sign_in_again(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    runner = Runner(backend, sail_logs=[REJECTED, ACCEPTED])
    original = runner.__call__

    def cancel_during_game(argv, env, **kwargs):
        code = original(argv, env, **kwargs)
        backend.ctx.cancel.cancel()
        return code

    monkeypatch_stream = cancel_during_game
    with contextlib.suppress(Exception):
        play(backend, monkeypatch_stream)

    assert runner.names == ["sail"]
    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()


def test_a_short_game_session_drops_the_blob(tmp_path, play, monkeypatch):
    """A server-side refusal shows in the game, after the init: the user closes it quickly."""
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 3600)
    runner = Runner(backend)

    play(backend, runner)

    assert runner.names == ["sail"]
    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 0)
    again = Runner(backend)
    play(backend, again)
    assert again.names == ["sail"]


def test_a_blob_without_an_expiry_is_never_reused(tmp_path, play):
    backend = make_install(tmp_path)
    no_expiry = {k: v for k, v in AUTH.items() if k != "token_expire_time"}
    play(backend, Runner(backend, auth=no_expiry))
    runner = Runner(backend, auth=no_expiry)

    play(backend, runner)

    assert runner.names == ["sail"]


def test_logout_removes_the_blob(tmp_path, play):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    backend.ctx.emit = lambda _event: None

    backend.logout()

    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()


def test_remove_profile_removes_the_blob(tmp_path, play, monkeypatch):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    blob = backend._session_cache() / h.GAME_AUTH_NAME
    assert blob.is_file()
    profile = backend._session_cache().parent
    monkeypatch.setattr(launcher_module.paths, "profiles_dir", lambda: profile.parent)

    assert NikkeBackend.forget_session_files(profile.name) is True

    assert not blob.exists()


# A blob the sign-in service minted in this same Play is as fresh as it gets: signing in again
# cannot produce a better one, so a short session keeps it (#1420 F4). A reused blob still goes.


def test_a_short_session_after_a_fresh_sign_in_keeps_the_blob(tmp_path, play, monkeypatch):
    backend = make_install(tmp_path)
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 3600)

    assert play(backend, Runner(backend)) == 0

    assert (backend._session_cache() / h.GAME_AUTH_NAME).is_file()
    again = Runner(backend)
    play(backend, again)
    assert again.names == ["sail"]  # reused; a short session with a reused blob still drops it
    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()


def test_a_short_session_after_a_resign_in_in_the_same_play_keeps_the_new_blob(
    tmp_path, play, monkeypatch
):
    backend = make_install(tmp_path)
    play(backend, Runner(backend))
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 3600)
    runner = Runner(backend, sail_logs=[REJECTED, ACCEPTED])

    play(backend, runner)

    assert runner.names == ["sail", "sail"]
    assert (backend._session_cache() / h.GAME_AUTH_NAME).is_file()


def test_a_fresh_blob_the_game_rejected_is_dropped_even_after_a_long_session(
    tmp_path, play, monkeypatch
):
    backend = make_install(tmp_path)
    monkeypatch.setattr(NikkeBackend, "_GAME_AUTH_KEEP_AFTER_SECONDS", 0)

    play(backend, Runner(backend, sail_logs=[REJECTED]))

    assert not (backend._session_cache() / h.GAME_AUTH_NAME).exists()
