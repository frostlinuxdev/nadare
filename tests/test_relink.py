# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointing a profile at a prefix the user moved by hand."""

from __future__ import annotations

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


def test_ownership_moves_only_when_the_old_folder_is_gone(tmp_path):
    old, new = tmp_path / "old", _prefix(tmp_path / "new")
    profile = {
        "prefix": str(old),
        "owned_trees": ["prefix"],
        "owned_paths": {"prefix": str(old)},
    }
    moved = relink.plan_relink(profile, new)  # old does not exist: it was moved
    assert moved.changes["owned_paths"] == {"prefix": str(new.resolve())}

    old.mkdir()  # still there: the user copied it, so the copy is not ours to delete
    copied = relink.plan_relink(profile, new)
    assert "owned_paths" not in copied.changes


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
    assert relink.plan_relink(profile, new).changes["installed_prefix"] == str(new)
    other = {"prefix": str(old), "installed_prefix": str(tmp_path / "somewhere")}
    assert "installed_prefix" not in relink.plan_relink(other, new).changes
