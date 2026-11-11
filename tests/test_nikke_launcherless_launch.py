# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the launcherless NIKKE launch path in ``NikkeBackend``.

Nothing here runs Wine: the runner command and the output stream are replaced.
What is checked is the decision (launcher vs. direct game), the order of the two
helpers, the session file the Sail host gets, and the cleanup.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendError, get

AUTH = {
    "openid": "10000000000000000001",
    "token": "0123456789abcdef0123456789abcdef01234567",
    "pf": "LevelInfinite_Example",
    "pf_key": "00000000000000000000000000000000",
    "bind_list": "[]",
}


def make_install(tmp_path, *, signed_in: bool = True, game: bool = True) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    launcher = write_exe(
        prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ"
    )
    write_exe(launcher.parent / "intl_service" / "intl_service.exe", "MZ")
    if game:
        write_exe(prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe", "MZ")
        (prefix / "drive_c" / "NIKKE" / "Unity" / "com_proximabeta_NIKKE").mkdir(parents=True)
    if signed_in:
        storage = h.cache_storage_dir(prefix)
        write_exe(storage / "intl_auth.txt", "session")
        write_exe(storage / "mmkv" / "mmkv.default", "device")
    values = {
        "prefix": str(prefix),
        "wine_binary": "/usr/bin/wine",
        "launcherless": True,
    }
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def stored_cache(backend: NikkeBackend):
    return h.cache_storage_dir(backend.prefix())


def test_the_option_exists_and_is_off_by_default():
    option = next(o for o in get("launcher-nikke").options if o.key == "launcherless")
    assert option.default is False
    assert "launcher" in option.help
    assert "sign-in" in option.help


def test_the_launcher_is_used_when_the_option_is_off(tmp_path):
    backend = make_install(tmp_path)
    backend.ctx.profile["launcherless"] = False
    assert backend._launcherless_wanted() is False


def test_plan_is_none_without_the_game_and_says_why(tmp_path):
    backend = make_install(tmp_path, game=False)
    notes = []
    backend.info = notes.append

    assert backend._launcherless_plan() is None
    assert any("not installed yet" in note for note in notes)


def test_plan_is_none_without_a_sign_in(tmp_path):
    backend = make_install(tmp_path, signed_in=False)
    notes = []
    backend.info = notes.append

    assert backend._launcherless_plan() is None
    assert any("no NIKKE sign-in" in note for note in notes)


def test_plan_adopts_the_launcher_cache_without_signing_in_again(tmp_path):
    backend = make_install(tmp_path)
    notes = []
    backend.info = notes.append

    files = backend._launcherless_plan()

    assert files is not None
    assert h.has_cache(backend._session_cache())
    assert (backend._session_cache() / "intl_auth.txt").read_bytes() == b"session"
    assert notes == []


def test_the_session_cache_is_private(tmp_path):
    backend = make_install(tmp_path)
    backend._launcherless_plan()

    cache = backend._session_cache()
    assert cache.stat().st_mode & 0o777 == 0o700
    assert (cache / "intl_auth.txt").stat().st_mode & 0o777 == 0o600


def test_patterns_include_the_game_only_during_a_launcherless_run(tmp_path):
    backend = make_install(tmp_path)
    assert "nikke.exe" not in backend._patterns().names

    backend._launcherless_active = True
    assert "nikke.exe" in backend._patterns().names
    assert h.SAIL_HOST_NAME in backend._patterns().names


def test_a_launcherless_launch_runs_the_intl_host_then_the_sail_host(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    backend._launcherless_plan()
    calls = []

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        calls.append(list(argv))
        if h.INTL_HOST_NAME in argv[0]:
            out = argv[argv.index("--out") + 1]
            # Turn the C:\ path the host got back into the file under the prefix.
            relative = out.split(":\\", 1)[1].replace("\\", "/")
            session_file = backend.prefix() / "drive_c" / relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        return 0

    monkeypatch.setattr(launcher_module, "stream", fake_stream)
    monkeypatch.setattr(
        NikkeBackend, "_prefix_command", lambda self, args, extra_env=None, **kw: (args, {})
    )
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(
        NikkeBackend,
        "_renderer_probe",
        lambda self: _NoProbe(),
    )
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )
    notes = []
    backend.info = notes.append

    code = backend.launch()

    assert code == 0
    assert len(calls) == 2
    assert h.INTL_HOST_NAME in calls[0][0]
    assert h.SAIL_HOST_NAME in calls[1][0]
    assert "--launch" in calls[1]
    # the Sail host got the real resource folder, not the capture's S: path
    assert not any("S:\\NIKKE" in arg for arg in calls[1])
    # the temporary session directory is gone after the run
    stage = backend.prefix().joinpath(*h.STAGE_PARTS)
    assert list(stage.glob("session-*")) == []


def test_a_failed_sign_in_says_how_to_recover(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    backend._launcherless_plan()

    monkeypatch.setattr(launcher_module, "stream", lambda *a, **k: 3)
    monkeypatch.setattr(
        NikkeBackend, "_prefix_command", lambda self, args, extra_env=None, **kw: (args, {})
    )
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    backend.info = lambda _line: None

    with pytest.raises(BackendError, match="sign in again in the launcher"):
        backend.launch()


def test_a_missing_cache_fails_before_anything_runs(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    files = backend._launcherless_plan()
    assert files is not None
    import shutil

    shutil.rmtree(backend._session_cache())
    shutil.rmtree(h.cache_storage_dir(backend.prefix()))

    with pytest.raises(BackendError, match="saved NIKKE sign-in is gone"):
        backend._launch_launcherless(files)


def test_an_expired_or_unreadable_session_says_what_to_do(tmp_path):
    backend = make_install(tmp_path)
    bad = tmp_path / "session.enc.json"
    bad.write_text("not json", encoding="utf-8")

    with pytest.raises(BackendError, match="sign in again in the launcher"):
        backend._read_host_session(bad)


class _NoProbe:
    def start(self):
        return self

    def stop(self):
        pass

    def join(self, timeout=None):
        pass

    result = None


# ---- round-1 review 2026-11-11 -------------------------------------------------------------------


def _age(path, seconds):
    import os
    import time

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_a_newer_sign_in_in_the_launcher_replaces_nadares_old_copy(tmp_path):
    """The advice after a failed launcherless start is "sign in again in the launcher"; the
    next launcherless Play must use that new sign-in, not restore the old one over it."""
    backend = make_install(tmp_path)
    assert backend._launcherless_plan() is not None  # adopts "session"
    own = backend._session_cache() / "intl_auth.txt"
    _age(own, 3600)
    storage = stored_cache(backend)
    (storage / "intl_auth.txt").write_text("fresh session")

    cache = backend._launcherless_cache()

    assert (cache / "intl_auth.txt").read_text() == "fresh session"
    h.restore_cache(cache, storage)  # what a launcherless Play does next
    assert (storage / "intl_auth.txt").read_text() == "fresh session"


def test_an_older_copy_in_the_prefix_does_not_replace_nadares(tmp_path):
    backend = make_install(tmp_path)
    backend._launcherless_plan()
    own = backend._session_cache() / "intl_auth.txt"
    own.write_text("nadare login")
    _age(stored_cache(backend) / "intl_auth.txt", 3600)

    cache = backend._launcherless_cache()

    assert (cache / "intl_auth.txt").read_text() == "nadare login"


def test_a_session_missing_its_fields_says_what_to_do(tmp_path):
    backend = make_install(tmp_path)
    bad = tmp_path / "session.enc.json"
    bad.write_text(json.dumps({"openid": "1"}), encoding="utf-8")
    with pytest.raises(BackendError, match="sign in again in the launcher"):
        backend._read_host_session(bad)


def test_an_empty_newer_sign_in_in_the_prefix_does_not_replace_nadares(tmp_path):
    """Round 2: a launcher sign-in that was killed can leave a newer but empty intl_auth.txt."""
    backend = make_install(tmp_path)
    backend._launcherless_plan()
    own = backend._session_cache() / "intl_auth.txt"
    _age(own, 3600)
    (stored_cache(backend) / "intl_auth.txt").write_text("")
    cache = backend._launcherless_cache()
    assert (cache / "intl_auth.txt").read_text() == "session"
