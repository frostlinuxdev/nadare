# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointing a profile at a prefix the user moved by hand."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import relink


def _prefix(root: Path, *, game: str = "") -> Path:
    (root / "drive_c").mkdir(parents=True)
    if game:
        (root / game).mkdir(parents=True)
    return root


def test_changes_the_prefix_and_reroots_an_install_inside_it(tmp_path):
    old, new = tmp_path / "old" / "prefix", tmp_path / "new" / "prefix"
    _prefix(new, game="drive_c/Game")
    profile = {"prefix": str(old), "install_dir": str(old / "drive_c" / "Game")}
    plan = relink.plan_relink(profile, new)
    assert plan.changes["prefix"] == str(new)
    assert plan.changes["install_dir"] == str(new / "drive_c" / "Game")


def test_an_install_outside_the_prefix_is_left_alone(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _prefix(new)
    profile = {"prefix": str(old), "install_dir": str(tmp_path / "games" / "G")}
    plan = relink.plan_relink(profile, new)
    assert "install_dir" not in plan.changes


def test_a_rerooted_install_must_exist_at_the_new_place(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _prefix(new)  # the game folder did not come along
    profile = {"prefix": str(old), "install_dir": str(old / "drive_c" / "Game")}
    with pytest.raises(relink.RelinkError, match="game folder"):
        relink.plan_relink(profile, new)


@pytest.mark.parametrize("make", [lambda p: p.mkdir(), lambda p: None])
def test_a_folder_without_drive_c_is_refused(tmp_path, make):
    target = tmp_path / "x"
    make(target)
    with pytest.raises(relink.RelinkError, match="drive_c"):
        relink.plan_relink({"prefix": str(tmp_path / "old")}, target)


def test_the_same_place_is_refused(tmp_path):
    here = _prefix(tmp_path / "p")
    with pytest.raises(relink.RelinkError, match="already"):
        relink.plan_relink({"prefix": str(here)}, here)


def test_a_proton_compat_folder_is_accepted(tmp_path):
    new = tmp_path / "compat"
    (new / "pfx" / "drive_c").mkdir(parents=True)
    plan = relink.plan_relink({"prefix": str(tmp_path / "old")}, new)
    assert plan.changes["prefix"] == str(new)


def test_a_hand_relink_never_passes_ownership_on(tmp_path):
    old, new = tmp_path / "old", _prefix(tmp_path / "new")
    profile = {
        "prefix": str(old),
        "owned_trees": ["prefix"],
        "owned_paths": {"prefix": str(old)},
    }
    # old does not exist: the user moved it, but Nadare cannot tell that the folder picked is the
    # one it made, so the picked folder is never marked as Nadare's (it could be their own).
    moved = relink.plan_relink(profile, new)
    assert moved.changes["owned_trees"] == []
    assert moved.changes["owned_paths"] == {}

    old.mkdir()  # still there: the user copied it, so the copy is not ours to delete either
    copied = relink.plan_relink(profile, new)
    assert copied.changes["owned_trees"] == [] and copied.changes["owned_paths"] == {}


def test_apply_writes_the_changes_without_touching_other_keys(tmp_path):
    profile = {"prefix": "/old", "title": "T"}
    relink.apply(profile, relink.Relink(changes={"prefix": "/new"}, notes=[]))
    assert profile == {"prefix": "/new", "title": "T"}


def test_a_move_nadare_made_keeps_ownership_even_though_the_old_folder_stays(tmp_path):
    old, new = tmp_path / "old", _prefix(tmp_path / "new")
    old.mkdir()
    profile = {
        "prefix": str(old),
        "owned_trees": ["prefix"],
        "owned_paths": {"prefix": str(old)},
    }
    plan = relink.plan_relink(profile, new, made_by_nadare=True)
    assert plan.changes["owned_paths"] == {"prefix": str(new.resolve())}


def test_the_recorded_installed_prefix_follows_the_move(tmp_path):
    old, new = tmp_path / "old", _prefix(tmp_path / "new")
    profile = {"prefix": str(old), "installed_prefix": str(old)}
    # Only Nadare's own move proves the new folder is the one it installed into.
    assert relink.plan_relink(profile, new, made_by_nadare=True).changes[
        "installed_prefix"
    ] == str(new)
    assert "installed_prefix" not in relink.plan_relink(profile, new).changes  # a hand pick
    other = {"prefix": str(old), "installed_prefix": str(tmp_path / "somewhere")}
    assert "installed_prefix" not in relink.plan_relink(other, new, made_by_nadare=True).changes


def test_install_dir_spelled_through_a_symlink_to_the_prefix_is_rerooted(tmp_path):
    real = tmp_path / "real" / "prefix"
    (real / "drive_c" / "Game").mkdir(parents=True)
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    profile = {"prefix": str(real), "install_dir": str(alias / "drive_c" / "Game")}
    new = tmp_path / "new" / "prefix"
    new.parent.mkdir()
    os.rename(real, new)
    plan = relink.plan_relink(profile, new)
    assert plan.changes["install_dir"] == str(new / "drive_c" / "Game")


def test_a_symlink_to_the_same_prefix_counts_as_the_same_place(tmp_path):
    here = _prefix(tmp_path / "p")
    alias = tmp_path / "alias"
    os.symlink(here, alias)
    with pytest.raises(relink.RelinkError, match="already"):
        relink.plan_relink({"prefix": str(here)}, alias)


def test_check_movable_refuses_a_folder_that_is_not_a_prefix(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(relink.RelinkError, match="drive_c"):
        relink.check_movable({"prefix": str(plain)})
    with pytest.raises(relink.RelinkError, match="no prefix"):
        relink.check_movable({"prefix": ""})


def test_check_movable_refuses_when_the_game_folder_inside_is_missing(tmp_path):
    pfx = _prefix(tmp_path / "p")
    with pytest.raises(relink.RelinkError, match="game folder"):
        relink.check_movable({"prefix": str(pfx), "install_dir": str(pfx / "drive_c" / "Gone")})


def test_check_movable_accepts_a_normal_profile(tmp_path):
    pfx = _prefix(tmp_path / "p", game="drive_c/Game")
    relink.check_movable({"prefix": str(pfx), "install_dir": str(pfx / "drive_c" / "Game")})


def test_a_hand_relink_of_the_whole_game_home_takes_the_rebuild_backup_along(tmp_path):
    old_home, new_home = tmp_path / "lib" / "Nadare" / "Game", tmp_path / "lib2" / "Nadare" / "Game"
    _prefix(new_home / "prefix")
    (new_home / "install").mkdir()
    (new_home / "prefix.broken-20261029-120000").mkdir()
    profile = {
        "prefix": str(old_home / "prefix"),
        "install_dir": str(old_home / "install"),
        "prefix_backups": [str(old_home / "prefix.broken-20261029-120000")],
    }
    plan = relink.plan_relink(profile, new_home / "prefix")
    assert plan.changes["prefix_backups"] == [str(new_home / "prefix.broken-20261029-120000")]


def test_a_backup_that_is_still_where_it_was_is_left_alone(tmp_path):
    old_home, new_home = tmp_path / "old", tmp_path / "new"
    _prefix(new_home / "prefix")
    kept = old_home / "prefix.broken-20261029-120000"
    kept.mkdir(parents=True)
    profile = {"prefix": str(old_home / "prefix"), "prefix_backups": [str(kept)]}
    plan = relink.plan_relink(profile, new_home / "prefix")
    assert "prefix_backups" not in plan.changes
