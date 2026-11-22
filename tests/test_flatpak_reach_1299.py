# SPDX-License-Identifier: GPL-3.0-or-later
"""#1299: an old game outside the Flatpak's folders says why, instead of "Not installed".

The Flatpak no longer sees the whole host (#1211/#1244). A game an older Nadare (the AppImage, or
an older Flatpak with ``--filesystem=host``) installed in ``/srv/games`` or ``/data`` simply does
not exist inside it, so its backend reports ``not_installed`` and the page offers INSTALL. The
check used only when a folder is saved now runs when the status is read and once at start-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import flatpakfs, flatpakreport, sandbox
from nadare.core.backends import Status

INFO = "[Application]\nname=io.github.frostlinuxdev.Nadare\n\n[Context]\nfilesystems={fs}\n"
TIER1 = "home;/mnt;/media;/run/media;xdg-data/umu:create;/run/udev:ro;"
FAR = "/srv/nadare-test-1299/Games/BD2"  # outside every grant, and absent on the test host


@pytest.fixture
def flatpak(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Games" / "BD2").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    info = tmp_path / "flatpak-info"
    info.write_text(INFO.format(fs=TIER1))
    monkeypatch.setattr(flatpakfs, "FLATPAK_INFO", info)
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    assert not Path(FAR).exists()
    return home


def _found(profiles):
    return [(f.profile, f.label, f.path) for f in flatpakreport.out_of_reach(profiles)]


# ---- which games ------------------------------------------------------------------------------
def test_a_game_folder_outside_the_grants_is_found(flatpak):
    assert _found({"bd2": {"backend": "direct-bd2", "install_dir": FAR}}) == [
        ("bd2", "game folder", FAR)
    ]


def test_prefix_and_wineprefix_under_extra_count(flatpak):
    profiles = {"w": {"backend": "wine", "extra": {"wineprefix": FAR}}}
    assert _found(profiles) == [("w", "prefix", FAR)]


def test_folders_the_flatpak_can_use_are_not_found(flatpak):
    inside = str(flatpak / "Games" / "BD2")
    assert _found({"bd2": {"backend": "direct-bd2", "install_dir": inside}}) == []
    assert _found({"bd2": {"backend": "direct-bd2"}}) == [], "no folder: Nadare's own"
    assert _found({"bd2": {"backend": "direct-bd2", "install_dir": "  "}}) == []


def test_a_folder_that_is_visible_after_all_is_not_found(flatpak, tmp_path, monkeypatch):
    """The grants are parsed by us; a folder the Flatpak can see is never called out of reach."""
    there = tmp_path / "srv" / "Games"
    there.mkdir(parents=True)
    assert flatpakfs.unreachable_reason(there) is not None, "outside the grants as parsed"
    assert _found({"bd2": {"backend": "direct-bd2", "install_dir": str(there)}}) == []


def test_outside_a_flatpak_nothing_is_found(flatpak, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    assert _found({"bd2": {"backend": "direct-bd2", "install_dir": FAR}}) == []


def test_one_entry_per_folder_and_bad_profiles_are_skipped(flatpak):
    profiles = {
        "bd2": {"backend": "direct-bd2", "install_dir": FAR, "prefix": FAR},
        "odd": "not a profile",
        "num": {"backend": "wine", "install_dir": 5},
    }
    assert _found(profiles) == [("bd2", "game folder", FAR)]


def test_unseen_and_the_message(flatpak):
    profiles = {"bd2": {"backend": "direct-bd2", "title": "Brown Dust 2", "install_dir": FAR}}
    found = flatpakreport.out_of_reach(profiles)
    assert flatpakreport.unseen(found, None) == found
    assert flatpakreport.unseen(found, [found[0].key]) == []
    text = flatpakreport.message(found)
    assert "Brown Dust 2" in text and FAR in text
    assert f"flatpak override --user --filesystem={FAR} {flatpakfs.APP_ID}" in text
    assert "restart" in text.lower()


# ---- the status --------------------------------------------------------------------------------
def test_not_installed_becomes_an_error_that_says_why(flatpak):
    profile = {"backend": "direct-bd2", "install_dir": FAR}
    status = Status("not_installed", message="Not installed", data={"can_install": True})
    got = flatpakreport.explain(profile, status)
    assert got.state == "error"
    assert got.data["out_of_reach"] == FAR
    assert got.data["can_install"] is True, "the backend's own data is kept"
    assert f"--filesystem={FAR}" in got.message
    assert status.state == "not_installed", "the backend's status object is not changed"


@pytest.mark.parametrize("state", ["installed", "running", "error"])
def test_other_states_are_left_alone(flatpak, state):
    profile = {"backend": "direct-bd2", "install_dir": FAR}
    status = Status(state, message="m")
    assert flatpakreport.explain(profile, status) is status


def test_a_reachable_not_installed_game_is_left_alone(flatpak):
    status = Status("not_installed", message="Not installed")
    profile = {"backend": "direct-bd2", "install_dir": str(flatpak / "Games" / "BD2")}
    assert flatpakreport.explain(profile, status) is status
    assert flatpakreport.explain(None, status) is status


def test_the_error_offers_retry_not_install(flatpak):
    from nadare.core.gamefacts import primary_operation

    profile = {"backend": "direct-bd2", "install_dir": FAR}
    status = flatpakreport.explain(profile, Status("not_installed", data={"can_install": True}))
    assert primary_operation(status) == "retry"


# ---- wiring ------------------------------------------------------------------------------------
class _Backend:
    def __init__(self, profile):
        from types import SimpleNamespace

        self.ctx = SimpleNamespace(profile=profile)

    def status(self):
        return Status("not_installed", message="Not installed")


def test_the_gui_status_worker_explains(flatpak):
    from nadare.gui import workers

    worker = workers.BackendWorker.__new__(workers.BackendWorker)
    worker._operation = "status"
    got = []

    class Sig:
        def emit(self, value):
            got.append(value)

    worker.result = Sig()
    workers.BackendWorker._dispatch(worker, _Backend({"backend": "wine", "install_dir": FAR}))
    assert [s.state for s in got] == ["error"]
    assert got[0].data["out_of_reach"] == FAR


def test_the_cli_status_explains(flatpak, tmp_path, capsys):
    from nadare import app
    from nadare.core.config import Config

    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("bd2", backend="direct-bd2", install_dir=FAR)
    cfg.save()
    code = app.run_cli(["-c", str(path), "status", "-p", "bd2"])
    out = capsys.readouterr().out
    assert code == 1
    assert "bd2: error" in out and f"--filesystem={FAR}" in out


# ---- start-up note -----------------------------------------------------------------------------
class _Window:
    """The pieces of MainWindow the start-up check touches."""

    def __init__(self, profiles, seen=None):
        from types import SimpleNamespace

        self.saved = 0
        self.config = SimpleNamespace(
            notice=None,
            profiles=profiles,
            data={"flatpak_reach_seen": list(seen or [])},
            save=self._save,
        )
        self.emitted = []
        self.flatpak_reach_found = SimpleNamespace(emit=self.emitted.append)

    def _save(self):
        self.saved += 1


def test_startup_lists_each_game_once(flatpak, monkeypatch):
    from nadare.core import config
    from nadare.gui.main_window_parts import cleanup

    assert config.TOP_DEFAULTS["flatpak_reach_seen"] == []
    shown = []
    monkeypatch.setattr(
        cleanup.QMessageBox, "warning", lambda *a, **k: shown.append(a[2]), raising=True
    )
    mixin = cleanup.CleanupMixin
    profiles = {"bd2": {"backend": "direct-bd2", "title": "Brown Dust 2", "install_dir": FAR}}
    win = _Window(profiles)
    mixin._run_flatpak_reach_check(win, profiles, [])
    assert len(win.emitted) == 1
    mixin._show_flatpak_reach(win, win.emitted[0])
    assert len(shown) == 1 and "Brown Dust 2" in shown[0]
    assert win.config.data["flatpak_reach_seen"] == [win.emitted[0][0].key]
    assert win.saved == 1

    again = _Window(profiles, seen=win.config.data["flatpak_reach_seen"])
    mixin._run_flatpak_reach_check(again, profiles, again.config.data["flatpak_reach_seen"])
    assert again.emitted == [], "told once"


def test_startup_says_nothing_outside_a_flatpak(flatpak, monkeypatch):
    from nadare.gui.main_window_parts import cleanup

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    profiles = {"bd2": {"backend": "direct-bd2", "install_dir": FAR}}
    win = _Window(profiles)
    cleanup.CleanupMixin._run_flatpak_reach_check(win, profiles, [])
    assert win.emitted == []
