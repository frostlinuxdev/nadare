# SPDX-License-Identifier: GPL-3.0-or-later
"""#1211 tier 1: the Flatpak no longer sees the whole host; Nadare says what it cannot reach.

The Flatpak's own ``/.flatpak-info`` lists what it may use (``[Context] filesystems=``), including
anything the user added with ``flatpak override``. A folder outside that list either does not exist
inside the Flatpak or comes back from the file picker as a portal path (``/run/user/<uid>/doc/…``);
games must not be installed into either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import flatpakfs, sandbox

INFO = """[Application]
name=io.github.frostlinuxdev.Nadare
runtime=runtime/org.freedesktop.Platform/x86_64/25.08

[Context]
shared=network;ipc;
filesystems={fs}
"""
TIER1 = (
    "home;/mnt;/media;/run/media;~/.steam;xdg-data/umu:create;xdg-run/gamescope-0:rw;/run/udev:ro;"
)


@pytest.fixture
def flatpak(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    info = tmp_path / "flatpak-info"

    def write(fs: str = TIER1) -> Path:
        info.write_text(INFO.format(fs=fs))
        return home

    monkeypatch.setattr(flatpakfs, "FLATPAK_INFO", info)
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    return write


def test_outside_a_flatpak_every_folder_is_reachable(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    assert flatpakfs.unreachable_reason("/data/Games") is None


@pytest.mark.parametrize(
    "where", ["~/Games", "/mnt/izuna/Games", "/media/usb/Games", "/run/media/me/disk/Games"]
)
def test_the_usual_game_places_are_reachable(flatpak, where):
    home = flatpak()
    path = where.replace("~", str(home))
    assert flatpakfs.unreachable_reason(path) is None


def test_a_folder_outside_the_granted_places_is_refused_with_the_fix(flatpak):
    flatpak()
    reason = flatpakfs.unreachable_reason("/data/Games")
    assert reason is not None
    fix = "flatpak override --user --filesystem=/data/Games io.github.frostlinuxdev.Nadare"
    assert fix in reason


def test_a_folder_the_user_allowed_with_an_override_is_reachable(flatpak):
    flatpak(TIER1 + "/data;")
    assert flatpakfs.unreachable_reason("/data/Games") is None


def test_a_portal_path_from_the_file_picker_is_refused(flatpak):
    flatpak()
    reason = flatpakfs.unreachable_reason("/run/user/1000/doc/1a2b3c4d/Games")
    assert reason is not None and "flatpak override" in reason


def test_a_read_only_grant_is_not_enough_for_games(flatpak):
    flatpak(TIER1 + "/srv/games:ro;")
    assert flatpakfs.unreachable_reason("/srv/games/Nadare") is not None


def test_host_access_reaches_everything(flatpak):
    flatpak("host;")
    assert flatpakfs.unreachable_reason("/data/Games") is None


def test_nadares_own_folder_is_always_reachable(flatpak, monkeypatch):
    home = flatpak("/mnt;")  # no home grant at all
    own = home / ".var" / "app" / "io.github.frostlinuxdev.Nadare" / "data" / "nadare"
    monkeypatch.setenv("FLATPAK_ID", "io.github.frostlinuxdev.Nadare")
    assert flatpakfs.unreachable_reason(own) is None


def test_a_tilde_grant_is_under_home(flatpak):
    home = flatpak("~/Games;")
    assert flatpakfs.unreachable_reason(home / "Games" / "Nadare") is None
    assert flatpakfs.unreachable_reason(home / "Other") is not None


def test_an_unreadable_flatpak_info_never_blocks(flatpak, monkeypatch, tmp_path):
    flatpak()
    monkeypatch.setattr(flatpakfs, "FLATPAK_INFO", tmp_path / "missing")
    assert flatpakfs.unreachable_reason("/data/Games") is None


def test_a_similar_prefix_is_not_a_grant(flatpak):
    flatpak("/mnt;")
    assert flatpakfs.unreachable_reason("/mntx/Games") is not None


# ---- where the check is used --------------------------------------------------------------


def test_adding_a_library_out_of_reach_is_refused(flatpak):
    from nadare.core import libraries
    from nadare.core.config import Config

    flatpak()
    cfg = Config.load(Path(os.environ["HOME"]) / "config.json")
    with pytest.raises(libraries.LibraryError, match="flatpak override"):
        libraries.add_library(cfg, "/data/Games")


def test_add_game_refuses_a_folder_out_of_reach_and_says_how(flatpak, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.add_game import AddGameDialog

    QApplication.instance() or QApplication([])
    flatpak()
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(cfg, data_root=tmp_path / "data", kind="direct-bd2")
    try:
        dialog.location_toggle.click()
        dialog.set_install_dir("/data/Games")
        assert dialog.create_profile() is None
        assert "flatpak override" in dialog.error_label.text()
        assert not Config.load(tmp_path / "config.json").profiles
    finally:
        dialog.deleteLater()


def test_the_manifest_no_longer_grants_the_whole_host():
    yaml = pytest.importorskip("yaml")

    manifest = (
        Path(__file__).resolve().parents[1]
        / "packaging/flatpak/io.github.frostlinuxdev.Nadare.yml"
    )
    args = yaml.safe_load(manifest.read_text())["finish-args"]
    assert "--filesystem=host" not in args
    for needed in (
        "--filesystem=home",
        "--filesystem=/mnt",
        "--filesystem=/media",
        "--filesystem=/run/media",
        "--filesystem=~/.var/app/com.valvesoftware.Steam",
    ):
        assert needed in args, needed
    # every place the manifest grants is one the folder check accepts
    grants = [a.split("=", 1)[1] for a in args if a.startswith("--filesystem=")]
    assert all(not g.startswith("host") for g in grants)


def test_host_os_is_not_access_to_everything(flatpak):
    flatpak("host-os;home;")
    assert flatpakfs.unreachable_reason("/data/Games") is not None


def test_the_fix_for_a_portal_path_names_the_real_folder_not_the_portal(flatpak):
    flatpak()
    reason = flatpakfs.unreachable_reason("/run/user/1000/doc/1a2b3c4d/Games")
    assert "--filesystem=/run/user" not in reason
    assert "<the real folder>" in reason
