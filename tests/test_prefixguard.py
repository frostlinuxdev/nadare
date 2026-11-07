# SPDX-License-Identifier: GPL-3.0-or-later
"""Where a Wine prefix or install folder may not be (#851, #850)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import prefixguard


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "user"
    folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    return folder


def test_the_filesystem_root_is_refused(home):
    assert "top of the filesystem" in prefixguard.unsafe_reason("/")


def test_a_mount_point_is_refused(home, monkeypatch, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(drive.resolve()) or real(p))
    assert "top of a drive" in prefixguard.unsafe_reason(drive)
    assert prefixguard.unsafe_reason(drive / "Nadare" / "Game" / "prefix") == ""


def test_home_and_what_contains_it_are_refused(home):
    assert "home folder" in prefixguard.unsafe_reason(home)
    assert "home folder" in prefixguard.unsafe_reason(home.parent)


def test_personal_folders_are_refused(home):
    for name in ("Documents", "Downloads", "Desktop", "Pictures", "Music", "Videos"):
        assert "personal folder" in prefixguard.unsafe_reason(home / name), name


def test_a_folder_inside_home_is_fine(home):
    assert prefixguard.unsafe_reason(home / "Games" / "Nadare" / "G" / "prefix") == ""
    assert prefixguard.unsafe_reason(home / ".local" / "share" / "nadare" / "p") == ""


def test_symlinks_are_resolved_before_judging(home, tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(home)
    assert "home folder" in prefixguard.unsafe_reason(link)


def test_another_games_folders_are_refused(home):
    profiles = {
        "a": {"backend": "wine", "prefix": str(home / "Games" / "A" / "prefix")},
        "b": {"backend": "wine", "install_dir": str(home / "Games" / "B")},
    }
    assert "already used" in prefixguard.unsafe_reason(
        home / "Games" / "A" / "prefix", profiles=profiles, own="c"
    )
    assert "already used" in prefixguard.unsafe_reason(
        home / "Games" / "A" / "prefix" / "sub", profiles=profiles, own="c"
    )
    assert "already used" in prefixguard.unsafe_reason(
        home / "Games", profiles=profiles, own="c"
    )  # it would hold both
    assert (
        prefixguard.unsafe_reason(home / "Games" / "A" / "prefix", profiles=profiles, own="a") == ""
    )  # the game's own folder is fine


def test_a_folder_that_is_not_a_prefix_and_holds_other_files_is_refused_as_a_prefix(home):
    folder = home / "stuff"
    folder.mkdir()
    (folder / "notes.txt").write_text("x")
    assert "other files" in prefixguard.unsafe_reason(folder, as_prefix=True)
    assert prefixguard.unsafe_reason(folder, as_prefix=False) == ""


@pytest.mark.parametrize("marker", ["drive_c", "pfx"])
def test_an_existing_wine_prefix_is_accepted_as_a_prefix(home, marker):
    folder = home / "old"
    (folder / marker).mkdir(parents=True)
    (folder / "extra.txt").write_text("x")
    assert prefixguard.unsafe_reason(folder, as_prefix=True) == ""


def test_an_empty_or_missing_folder_is_accepted_as_a_prefix(home):
    assert prefixguard.unsafe_reason(home / "new", as_prefix=True) == ""
    (home / "empty").mkdir()
    assert prefixguard.unsafe_reason(home / "empty", as_prefix=True) == ""


def test_blank_input_has_no_opinion(home):
    assert prefixguard.unsafe_reason("") == ""
    assert prefixguard.unsafe_reason(None) == ""


def test_looks_like_a_wine_prefix(home):
    (home / "p" / "drive_c").mkdir(parents=True)
    assert prefixguard.is_wine_prefix(home / "p")
    (home / "q").mkdir()
    assert not prefixguard.is_wine_prefix(home / "q")
    (home / "r").mkdir()
    (home / "r" / "system.reg").write_text("x")
    assert prefixguard.is_wine_prefix(home / "r")


def test_broad_means_home_its_ancestors_a_hidden_mount_or_a_drive(home, monkeypatch):
    assert prefixguard.too_broad_to_bind(home, home)
    assert prefixguard.too_broad_to_bind(home.parent, home)
    assert prefixguard.too_broad_to_bind(Path("/"), home)
    assert not prefixguard.too_broad_to_bind(home / "Games" / "A", home)
