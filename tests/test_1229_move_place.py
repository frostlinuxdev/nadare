# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: Move and "I moved it myself" use the same place rule and Flatpak check as Add Game."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import flatpakfs, relink


def _managed(lib: Path) -> dict:
    home = lib / "Nadare" / "Game"
    (home / "prefix" / "drive_c").mkdir(parents=True)
    (home / "install").mkdir()
    return {
        "backend": "direct-bd2",
        "title": "Game",
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
    }


def test_a_move_into_the_home_folder_is_refused(tmp_path):
    profile = _managed(tmp_path / "lib")
    with pytest.raises(relink.RelinkError, match="home folder"):
        relink.check_home_target(profile, Path.home())


def test_a_move_into_a_private_folder_is_refused(tmp_path):
    profile = _managed(tmp_path / "lib")
    private = Path.home() / ".config"
    private.mkdir(parents=True, exist_ok=True)
    with pytest.raises(relink.RelinkError, match="private"):
        relink.check_home_target(profile, private)


def test_a_move_into_a_drive_top_library_is_fine(tmp_path, monkeypatch):
    profile = _managed(tmp_path / "lib")
    drive = tmp_path / "drive"
    drive.mkdir()
    real = relink.prefixguard.os.path.ismount
    monkeypatch.setattr(
        relink.prefixguard.os.path, "ismount", lambda p: str(p) == str(drive) or real(p)
    )
    relink.check_home_target(profile, drive)  # no error


def test_a_move_the_flatpak_cannot_reach_is_refused(tmp_path, monkeypatch):
    profile = _managed(tmp_path / "lib")
    target = tmp_path / "far"
    target.mkdir()
    monkeypatch.setattr(
        flatpakfs, "unreachable_reason", lambda f: "out of reach" if "far" in str(f) else None
    )
    with pytest.raises(relink.RelinkError, match="out of reach"):
        relink.check_home_target(profile, target)


def test_relinking_to_a_prefix_in_a_refused_place_is_refused(tmp_path, monkeypatch):
    profile = _managed(tmp_path / "lib")
    home = Path.home()
    (home / "drive_c").mkdir(parents=True, exist_ok=True)  # home itself "looks like" a prefix
    with pytest.raises(relink.RelinkError, match="home folder"):
        relink.plan_relink(profile, home)


def test_relinking_to_a_prefix_the_flatpak_cannot_reach_is_refused(tmp_path, monkeypatch):
    profile = _managed(tmp_path / "lib")
    moved = tmp_path / "far" / "prefix"
    (moved / "drive_c").mkdir(parents=True)
    monkeypatch.setattr(
        flatpakfs, "unreachable_reason", lambda f: "out of reach" if "far" in str(f) else None
    )
    with pytest.raises(relink.RelinkError, match="out of reach"):
        relink.plan_relink(profile, moved)


def test_check_place_is_quiet_for_an_ordinary_folder(tmp_path):
    relink.check_place(tmp_path / "Games" / "Nadare" / "Game")


# ---- one folder name for a title -------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["Re:Zero  Infinity", "A/B\\C", 'Q*u?o"t<e>s|', "  Spaces. ", "..", ""]
)
def test_install_and_move_name_a_game_folder_the_same_way(title, tmp_path):
    from nadare.core import libraries
    from nadare.gui import add_game

    expected = libraries.game_folder_name(title)
    assert add_game._folder_name(title) == expected
    profiles_dir = relink.paths.profiles_dir() / "some-id"
    named = title.strip(" ./\\:*?\"<>|")
    assert relink.home_name({"title": title}, profiles_dir) == (expected if named else "some-id")
    assert "/" not in expected and ":" not in expected and expected not in ("", ".", "..")
