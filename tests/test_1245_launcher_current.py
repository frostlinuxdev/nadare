# SPDX-License-Identifier: GPL-3.0-or-later
"""#1245: launcher-free NIKKE keeps the launcher folder (its sign-in service) current too."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend

KEYS = launcher_module.nikke_source.LAUNCHER_KEY_FILES
OLD = {k: b"old " + k.encode() for k in KEYS}
NEW = {k: b"new " + k.encode() for k in KEYS}
CLIENT = b"MZ current nikke.exe"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    return tmp_path


def _nikke(tmp_path, *, launcherless=True, files=OLD):
    prefix = tmp_path / "prefix"
    launcher = prefix / "drive_c" / "NIKKE" / "Launcher"
    write_exe(launcher / "nikke_launcher.exe", "MZ")
    for rel, data in files.items():
        (launcher / rel).parent.mkdir(parents=True, exist_ok=True)
        (launcher / rel).write_bytes(data)
    game = prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe"
    game.parent.mkdir(parents=True, exist_ok=True)
    game.write_bytes(CLIENT)
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": launcherless}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values)), launcher


@pytest.fixture
def api(monkeypatch):
    state = {"published": dict(NEW), "launcher_calls": 0, "installs": [], "games": [],
             "fail_install": False}

    def client_info(post=None):
        return {"version_name": "NIKKE_1", "version_id": "1", "_exe": CLIENT}

    def client_exe_entry(info, fetch=None):
        return SimpleNamespace(size=len(CLIENT), md5=hashlib.md5(CLIENT).digest())

    def launcher_info(post=None):
        state["launcher_calls"] += 1
        return {"version_name": "nikke_launcher_0.0.6.841"}

    def launcher_key_entries(info, fetch=None):
        return {
            rel: SimpleNamespace(size=len(data), md5=hashlib.md5(data).digest())
            for rel, data in state["published"].items()
        }

    def install_launcher(dest, *, emit=None, cancel=None, post=None, fetch=None):
        state["installs"].append(dest)
        if state["fail_install"]:
            raise launcher_module.railrepo.RailError("cut")
        for rel, data in state["published"].items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_bytes(data)

    def install_game(dest, *, emit=None, cancel=None, post=None, fetch=None):
        state["games"].append(dest)

    src = launcher_module.nikke_source
    for name, fn in (("client_info", client_info), ("client_exe_entry", client_exe_entry),
                     ("launcher_info", launcher_info),
                     ("launcher_key_entries", launcher_key_entries),
                     ("install_launcher", install_launcher), ("install_game", install_game)):
        monkeypatch.setattr(src, name, fn)
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    return state


def test_an_outdated_launcher_is_updated_before_a_launcherless_play(env, api):
    backend, launcher = _nikke(env)
    backend._update_before_play_locked()
    assert api["installs"] == [launcher]
    assert api["games"] == []  # the client was current: only the launcher was fetched
    for rel, data in NEW.items():
        assert (launcher / rel).read_bytes() == data
    assert not backend._launcher_update_marker().exists()


def test_a_current_launcher_is_left_alone(env, api):
    backend, launcher = _nikke(env, files=NEW)
    backend._update_before_play_locked()
    assert api["installs"] == []


def test_launcher_mode_never_touches_the_launcher(env, api):
    backend, launcher = _nikke(env, launcherless=False)
    backend.check_update()
    assert api["launcher_calls"] == 0 and api["installs"] == []


def test_an_interrupted_launcher_update_is_finished_next_play(env, api):
    backend, launcher = _nikke(env, files=NEW)
    api["fail_install"] = True
    with pytest.raises(Exception):  # noqa: B017 - BackendError from the cut update
        backend._update_launcher_direct(launcher)
    assert backend._launcher_update_marker().exists()
    api["fail_install"] = False
    backend._update_before_play_locked()
    assert len(api["installs"]) == 2
    assert not backend._launcher_update_marker().exists()


def test_the_status_counts_an_outdated_launcher_as_an_update(env, api):
    backend, launcher = _nikke(env)
    backend.check_update()
    status = SimpleNamespace(data={})
    backend._client_update_status(status)
    assert status.data["update_available"] is True


def test_update_brings_the_launcher_current_too(env, api):
    backend, launcher = _nikke(env)
    backend.check_update()
    backend.update()
    assert api["installs"] == [launcher]


def test_nothing_updates_under_a_running_game(env, api, monkeypatch):
    backend, launcher = _nikke(env)
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [1234])
    backend._update_before_play()
    assert api["installs"] == []


def test_an_unusual_launcher_folder_is_not_updated(env, api, monkeypatch):
    backend, launcher = _nikke(env)
    monkeypatch.setattr(backend, "find_launcher", lambda: env / "elsewhere" / "nikke_launcher.exe")
    backend._update_before_play_locked()
    assert api["installs"] == []


def test_launcher_mode_never_updates_the_launcher_even_when_asked(env, api):
    backend, launcher = _nikke(env, launcherless=False)
    backend._update_launcher_if_needed(fetch=True)
    assert api["installs"] == [] and api["launcher_calls"] == 0


def test_an_interrupted_update_is_finished_even_with_update_checks_off(env, api):
    backend, launcher = _nikke(env, files=NEW)
    backend._mark_update_incomplete(backend._launcher_update_marker())
    backend.ctx.config.data["check_updates"] = False
    backend._update_before_play_locked()
    assert api["installs"] == [launcher] and api["launcher_calls"] <= 1
    assert not backend._launcher_update_marker().exists()
