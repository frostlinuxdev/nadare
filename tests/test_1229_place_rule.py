# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: one unsafe-folder rule shared by libraries, game folders and the Sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import gamesandbox, libraries, prefixguard
from nadare.core.config import Config


@pytest.fixture()
def home() -> Path:
    path = Path.home()  # conftest gives every test its own HOME
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize(
    "rel",
    ["", "Documents", "Downloads", ".ssh", ".config", ".local", ".cache", ".local/share"],
)
def test_the_same_places_are_refused_everywhere(home, rel):
    folder = home / rel if rel else home
    folder.mkdir(parents=True, exist_ok=True)
    assert prefixguard.place_reason(folder)
    assert prefixguard.unsafe_reason(folder)
    assert gamesandbox._broad_reason(folder, home.resolve())
    with pytest.raises(libraries.LibraryError):
        libraries.add_library(Config.load(home.parent / "c.json"), folder)


def test_root_and_a_folder_holding_home_are_refused(home):
    assert prefixguard.place_reason(Path("/"))
    assert prefixguard.place_reason(home.parent)


@pytest.mark.parametrize("rel", ["Games", "Documents/Games", ".ssh2", "Games/Nadare/X"])
def test_ordinary_folders_are_fine(home, rel):
    folder = home / rel
    folder.mkdir(parents=True, exist_ok=True)
    assert prefixguard.place_reason(folder) == ""


def test_a_drive_top_may_be_a_library_but_not_a_game_folder(monkeypatch, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    real_ismount = prefixguard.os.path.ismount
    monkeypatch.setattr(
        prefixguard.os.path, "ismount", lambda p: str(p) == str(drive) or real_ismount(p)
    )
    assert prefixguard.place_reason(drive, allow_drive_top=True) == ""
    assert prefixguard.place_reason(drive)
    assert prefixguard.unsafe_reason(drive)


def test_a_folder_inside_another_librarys_nadare_folder_is_not_a_new_library(home):
    config = Config.load(home.parent / "c.json")
    lib = home / "Games"
    (lib / libraries.NADARE_DIR / "Some Game").mkdir(parents=True)
    libraries.add_library(config, lib)
    with pytest.raises(libraries.LibraryError, match="inside the library"):
        libraries.add_library(config, lib / libraries.NADARE_DIR / "Some Game")


def test_the_filesystem_root_is_named_as_such():
    assert prefixguard.place_reason(Path("/")) == "the top of the filesystem"


def test_a_drive_top_can_be_added_as_a_library(monkeypatch, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    real_ismount = prefixguard.os.path.ismount
    monkeypatch.setattr(
        prefixguard.os.path, "ismount", lambda p: str(p) == str(drive) or real_ismount(p)
    )
    config = Config.load(tmp_path / "c.json")
    assert libraries.add_library(config, drive) == drive.resolve()
