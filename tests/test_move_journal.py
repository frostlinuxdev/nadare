# SPDX-License-Identifier: GPL-3.0-or-later
"""A Move interrupted by a crash is finished at the next start (#1366 B-F2).

Found by the storage simulator: 115 of 1000 random runs crashed between the move and the
profile save, leaving the game pointing at a folder that was gone.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import relink, relocate


def _prefix(root: Path) -> Path:
    (root / "drive_c").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def test_a_finished_rename_whose_save_was_lost_is_pointed_at_the_new_place(tmp_path):
    old = tmp_path / "a" / "Game"
    new = tmp_path / "b" / "Game"
    profile = {
        "prefix": str(old / "prefix"),
        "install_dir": str(old / "install"),
        "owned_paths": {"prefix": str(old / "prefix")},
    }
    _prefix(old / "prefix")
    relocate.begin_move(profile, old, new)
    new.parent.mkdir(parents=True)
    old.rename(new)  # the rename happened, the save did not
    notes = relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(new / "prefix")
    assert profile["install_dir"] == str(new / "install")
    assert profile["owned_paths"]["prefix"] == str(new / "prefix")
    assert relocate.MOVE_IN_PROGRESS not in profile and notes


def test_the_original_still_there_keeps_the_game_where_it_was(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    partial = new.with_name(new.name + relocate.PARTIAL_SUFFIX)
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    _prefix(partial)  # a copy the crash cut short
    relocate.record_partial(profile[relocate.MOVE_IN_PROGRESS], partial)
    relocate.finish_interrupted_moves({"g": profile})
    assert profile["prefix"] == str(old)
    assert not partial.exists()  # Nadare's own unfinished copy (#1366 A8)
    assert relocate.MOVE_IN_PROGRESS not in profile


def test_a_finished_copy_next_to_the_original_is_reported_not_deleted(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    new = _prefix(tmp_path / "b" / "prefix")
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    notes = relocate.finish_interrupted_moves({"g": profile})
    assert new.is_dir() and old.is_dir() and profile["prefix"] == str(old)
    assert any("copy" in n for n in notes)


def test_running_it_twice_changes_nothing_more(tmp_path):
    old, new = _prefix(tmp_path / "a" / "prefix"), tmp_path / "b" / "prefix"
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, new)
    new.parent.mkdir(parents=True)
    old.rename(new)
    relocate.finish_interrupted_moves({"g": profile})
    once = dict(profile)
    assert relocate.finish_interrupted_moves({"g": profile}) == []
    assert profile == once


@pytest.mark.parametrize(
    "record", ["junk", {"from": "", "to": "/x"}, {"from": "/a\x00", "to": "/b"}]
)
def test_a_damaged_record_is_dropped(record):
    profile = {"prefix": "/somewhere", relocate.MOVE_IN_PROGRESS: record}
    relocate.finish_interrupted_moves({"g": profile})
    assert relocate.MOVE_IN_PROGRESS not in profile and profile["prefix"] == "/somewhere"


def test_move_refuses_a_prefix_reached_through_a_link(tmp_path):
    """Found by the simulator: profiles/<name> linked to Documents moved ~/Documents/prefix."""
    documents = _prefix(tmp_path / "Documents" / "prefix").parent
    from nadare.core import paths

    linked = paths.profiles_dir() / "linked"
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(documents)
    with pytest.raises(relink.RelinkError, match="through a link"):
        relink.check_movable({"prefix": str(linked / "prefix")})
    real = _prefix(tmp_path / "real" / "prefix")
    relink.check_movable({"prefix": os.fspath(real)})  # a real folder still moves


def test_move_allows_a_link_above_nadares_own_folders(tmp_path):
    """Review of #1389: ~/Games on another disk, or /home -> /var/home (Fedora Atomic), still moves.

    Renaming through a linked parent moves the real game folder; only a link inside Nadare's own
    folders points somewhere else.
    """
    (tmp_path / "ssd").mkdir()
    games = tmp_path / "Games"
    games.symlink_to(tmp_path / "ssd")
    _prefix(tmp_path / "ssd" / "Nadare" / "Game" / "prefix")
    home = games / "Nadare" / "Game"
    relink.check_movable({"prefix": str(home / "prefix")})
    relink.check_movable(
        {"prefix": str(home / "prefix"), "owned_trees": ["prefix"], "owned_dirs": [str(home)]}
    )


def test_move_refuses_a_link_inside_a_recorded_game_folder(tmp_path):
    """Nadare/<Game>/prefix linked out to the player's files is not Nadare's to move."""
    elsewhere = _prefix(tmp_path / "Documents" / "prefix")
    home = tmp_path / "Games" / "Nadare" / "Game"
    home.mkdir(parents=True)
    (home / "prefix").symlink_to(elsewhere)
    with pytest.raises(relink.RelinkError, match="link"):
        relink.check_movable(
            {"prefix": str(home / "prefix"), "owned_trees": ["prefix"], "owned_dirs": [str(home)]}
        )
    inner = tmp_path / "Games2" / "Nadare" / "Game"
    (inner / "pfx").mkdir(parents=True)
    (inner / "pfx" / "deep").symlink_to(elsewhere.parent)
    with pytest.raises(relink.RelinkError, match="through a link"):
        relink.check_movable(
            {
                "prefix": str(inner / "pfx" / "deep" / "prefix"),
                "owned_trees": ["prefix"],
                "owned_dirs": [str(inner)],
            }
        )
