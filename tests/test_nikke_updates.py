# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE client updates without the launcher (user, 2026-11-10).

The publisher's anonymous GetVersion API gives the current client (``version_name``), the CRC32
of ``NIKKE\\game\\nikke.exe`` (``key_file_info``) and every file's size + MD5; ``install_game``
over an install keeps matching files, so it is an update that fetches only what changed. Checked
live: the rig's launcher-installed client matched all 859 files. Only with "Start the game
without the NIKKE launcher" on; otherwise the launcher updates the game as before.
"""

from __future__ import annotations

import contextlib
import hashlib
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core.backends import BackendError

EXE_BYTES = b"MZ current nikke.exe"
NAME = "NIKKE.PC_Offcial_GL_153.6.10"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    return tmp_path


def _nikke(tmp_path, *, launcherless=True, exe=EXE_BYTES, **profile):
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    game = prefix / "drive_c" / "NIKKE" / "NIKKE" / "game" / "nikke.exe"
    game.parent.mkdir(parents=True, exist_ok=True)
    game.write_bytes(exe)
    values = {
        "prefix": str(prefix),
        "wine_binary": "/usr/bin/wine",
        "launcherless": launcherless,
        **profile,
    }
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def api(monkeypatch):
    """The publisher API, faked: count calls; publish the current exe by default.

    ``digest`` is the published nikke.exe (size, MD5) from the client manifest. The reply's
    ``key_file_info`` CRC is not a plain CRC32 of the file (measured), so it is not used.
    """
    state = {"calls": 0, "exe": EXE_BYTES, "name": NAME, "fail": False}

    def client_info(post=None):
        state["calls"] += 1
        if state["fail"]:
            raise launcher_module.railrepo.RailError("offline")
        return {"version_name": state["name"], "version_id": "0.0.0.74", "_exe": state["exe"]}

    def client_exe_entry(info, fetch=None):
        data = info["_exe"]
        return SimpleNamespace(size=len(data), md5=hashlib.md5(data).digest())

    monkeypatch.setattr(launcher_module.nikke_source, "client_info", client_info)
    monkeypatch.setattr(launcher_module.nikke_source, "client_exe_entry", client_exe_entry)
    return state


@pytest.fixture
def installs(monkeypatch):
    calls = []

    def install_game(dest, *, emit=None, cancel=None, post=None, fetch=None):
        calls.append(dest)
        # the update brings the new nikke.exe
        (dest / "NIKKE" / "game" / "nikke.exe").write_bytes(b"MZ new nikke.exe")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", install_game)
    return calls


# ---- check_update and status -----------------------------------------------------------------


def test_a_current_client_has_no_update(env, api):
    backend = _nikke(env)
    backend.check_update()
    assert api["calls"] == 1
    assert backend.status().data.get("update_available") is False


def test_an_outdated_client_shows_the_update_and_what_it_brings(env, api):
    api["exe"] = b"a newer nikke.exe"
    api["name"] = "NIKKE.PC_Offcial_GL_154.0.0"
    backend = _nikke(env)
    backend.check_update()
    data = backend.status().data
    assert data["update_available"] is True
    assert data["latest_version"] == "154.0.0"


def test_the_api_is_asked_at_most_every_few_hours(env, api):
    backend = _nikke(env)
    backend.check_update()
    backend.check_update()
    assert api["calls"] == 1


def test_no_check_when_update_checks_are_off(env, api):
    backend = _nikke(env)
    backend.ctx.config.data["check_updates"] = False
    backend.check_update()
    assert api["calls"] == 0


def test_no_check_and_no_update_flag_with_the_launcher(env, api):
    backend = _nikke(env, launcherless=False)
    backend.check_update()
    assert api["calls"] == 0
    assert "update_available" not in backend.status().data


def test_an_offline_check_changes_nothing(env, api):
    api["fail"] = True
    backend = _nikke(env)
    backend.check_update()  # must not raise
    assert backend.status().data.get("update_available") is False


def test_status_never_asks_the_network(env, api):
    backend = _nikke(env)
    backend.status()
    assert api["calls"] == 0


# ---- update -----------------------------------------------------------------------------------


def test_update_downloads_only_through_nadare_with_launcherless_on(env, api, installs, monkeypatch):
    monkeypatch.setattr(
        WineGameBackend, "update", lambda self: pytest.fail("the launcher was started")
    )
    api["exe"] = b"MZ new nikke.exe"  # the publisher released a new client
    backend = _nikke(env)
    backend.check_update()
    assert backend.status().data["update_available"] is True
    backend.update()
    assert installs == [backend._launcher_root()]
    # the update brought the published client, and the cache was refreshed by it
    assert backend.status().data["update_available"] is False


def test_update_uses_the_launcher_with_launcherless_off(env, api, installs, monkeypatch):
    started = []
    monkeypatch.setattr(WineGameBackend, "update", lambda self: started.append(1))
    _nikke(env, launcherless=False).update()
    assert started == [1]
    assert installs == []


def test_a_failed_download_says_what_to_do(env, api, monkeypatch):
    def broken(dest, **_kw):
        raise launcher_module.railrepo.RailError("chunk md5 mismatch")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", broken)
    with pytest.raises(BackendError, match="launcher"):
        _nikke(env).update()


# ---- a launcherless Play on an outdated client --------------------------------------------------


def _fake_launcherless(monkeypatch, ran):
    monkeypatch.setattr(NikkeBackend, "_launcherless_plan", lambda self: object())
    monkeypatch.setattr(
        NikkeBackend, "_launch_launcherless", lambda self, files: ran.append("game") or 0
    )


def test_an_outdated_client_is_updated_before_a_launcherless_play(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    backend = _nikke(env)
    assert backend.launch() == 0
    assert installs == [backend._launcher_root()]
    assert ran == ["game"]


def test_a_current_client_plays_without_downloading(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    backend = _nikke(env)
    assert backend.launch() == 0
    assert installs == []
    assert ran == ["game"]


def test_an_offline_version_check_still_plays(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["fail"] = True
    notes = []
    backend = _nikke(env)
    backend.info = notes.append
    assert backend.launch() == 0
    assert installs == []
    assert ran == ["game"]
    assert any("could not check" in n for n in notes)


def test_the_check_before_play_ignores_the_few_hours_cache(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    backend = _nikke(env)
    backend.check_update()  # cached: current
    api["exe"] = b"a newer nikke.exe"  # then the publisher releases an update
    with contextlib.suppress(Exception):
        backend.launch()
    assert installs == [backend._launcher_root()]


# ---- the page asks for NIKKE's version too -------------------------------------------------------


def test_the_page_runs_the_update_check_for_launcherless_nikke(env):
    from types import SimpleNamespace

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    cfg = Config.load(env / "config.json")
    cfg.add_profile("nikke", backend="launcher-nikke", launcherless=True)
    cfg.add_profile("nikke-l", backend="launcher-nikke")
    cfg.add_profile("nikke-x", backend="launcher-nikke", extra={"launcherless": True})
    window = SimpleNamespace(config=cfg)
    assert MainWindow._has_direct_manifest(window, "nikke") is True
    assert MainWindow._has_direct_manifest(window, "nikke-l") is False
    assert MainWindow._has_direct_manifest(window, "nikke-x") is True


def test_a_different_size_alone_counts_as_outdated(env, api):
    api["exe"] = EXE_BYTES + b"!"
    backend = _nikke(env)
    backend.check_update()
    assert backend.status().data["update_available"] is True


# ---- round-1 review 2026-11-11 -------------------------------------------------------------------


def test_no_update_download_when_play_falls_back_to_the_launcher(env, api, installs, monkeypatch):
    """No sign-in yet: the launcher opens and updates the game itself; no 1 GB download first."""
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(NikkeBackend, "_launcherless_plan", lambda self: None)
    monkeypatch.setattr(NikkeBackend, "_launch_with_watch", lambda self: 0)
    backend = _nikke(env)
    assert backend.launch() == 0
    assert installs == []
    assert api["calls"] == 0


def test_play_skips_the_publisher_when_update_checks_are_off(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    backend = _nikke(env)
    backend.ctx.config.data["check_updates"] = False
    assert backend.launch() == 0
    assert api["calls"] == 0
    assert installs == []
    assert ran == ["game"]


def test_an_interrupted_update_is_finished_before_the_next_play(env, api, monkeypatch):
    """nikke.exe may already be new when a later file failed: it alone cannot say "current"."""
    ran = []
    _fake_launcherless(monkeypatch, ran)
    calls = []

    def half_update(dest, **_kw):
        calls.append(dest)
        (dest / "NIKKE" / "game" / "nikke.exe").write_bytes(api["exe"])  # new exe written first
        if len(calls) == 1:
            raise launcher_module.railrepo.RailError("connection reset")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", half_update)
    api["exe"] = b"a newer nikke.exe"
    backend = _nikke(env)
    with pytest.raises(BackendError):
        backend.update()
    backend.check_update()
    assert backend.status().data["update_available"] is True  # not "current"
    assert backend.launch() == 0
    assert len(calls) == 2  # Play finished the update
    assert backend.status().data["update_available"] is False


def test_update_refuses_while_the_game_runs(env, api, installs, monkeypatch):
    api["exe"] = b"a newer nikke.exe"
    backend = _nikke(env)
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [4242])
    with pytest.raises(BackendError, match="Stop the game"):
        backend.update()
    assert installs == []


def test_the_update_marker_never_writes_through_a_link(env, api, installs, monkeypatch, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    api["exe"] = b"MZ new nikke.exe"
    backend = _nikke(env)
    backend._update_marker().symlink_to(victim)
    backend.update()
    assert victim.read_text() == "precious"
    assert not backend._update_marker().exists()


def test_update_refuses_while_another_instance_runs_the_game(env, api, installs, monkeypatch):
    """Round 2: another Flatpak instance's game is invisible to /proc; the run lock says so."""
    api["exe"] = b"a newer nikke.exe"
    backend = _nikke(env)
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    monkeypatch.setattr(NikkeBackend, "_running_elsewhere", lambda self: True)
    with pytest.raises(BackendError, match="Stop the game"):
        backend.update()
    assert installs == []


def test_play_does_not_update_under_another_instances_game(env, api, installs, monkeypatch):
    """Round 3: the Flatpak run-lock guard was on Update only; Play updated first, then waited."""
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(NikkeBackend, "_running_elsewhere", lambda self: True)
    backend = _nikke(env)
    with contextlib.suppress(Exception):
        backend.launch()
    assert installs == []
