# SPDX-License-Identifier: GPL-3.0-or-later
"""Relinking by hand must never make Nadare "own" a folder (move/prefix analysis, finding 1).

A hand relink used to copy the ownership record onto whatever folder the user picked once the old
one was gone, so a mis-picked ``~/.wine`` became deletable. Only Nadare's own move transfers
ownership now. The same plan stores a clean absolute path, follows a symlinked ``installed_prefix``,
flags a prefix another game uses, and says when the game folder was left behind.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from nadare.core import relink, removal
from nadare.core.config import Config


def _prefix(path: Path) -> Path:
    (path / "drive_c" / "windows").mkdir(parents=True)
    return path


def _profile(tmp_path: Path) -> tuple[dict, Path, Path]:
    """The profile Add Game makes: ``Nadare/<Game>/{install,prefix}`` with ownership recorded."""
    home = tmp_path / "lib" / "Nadare" / "Game"
    prefix = _prefix(home / "prefix")
    install = home / "install"
    install.mkdir()
    profile = {
        "backend": "arknights-yostar",
        "title": "Game",
        "prefix": str(prefix),
        "install_dir": str(install),
        "installed_prefix": str(prefix),
        "owned_dirs": [str(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": str(install.resolve()),
            "prefix": str(prefix.resolve()),
        },
    }
    return profile, prefix, install


def _moved_by_hand(profile: dict, prefix: Path, tmp_path: Path) -> Path:
    new = tmp_path / "elsewhere" / "pfx"
    new.parent.mkdir()
    shutil.move(str(prefix), str(new))
    return new


def _trees(tmp_path: Path, profile: dict) -> dict[str, removal.RemovableTree]:
    config = Config.load(tmp_path / "config.json")
    config.add_profile("game", **profile)
    return {t.key: t for t in removal.removable_trees(config, "game")}


# ---- ownership ---------------------------------------------------------------------------------
def test_a_hand_relink_never_makes_the_new_folder_owned(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new)
    relink.apply(profile, plan)
    assert "prefix" not in profile["owned_trees"]
    assert "prefix" not in profile.get("owned_paths", {})
    assert not _trees(tmp_path, profile)["prefix"].safe


def test_a_mis_picked_prefix_the_user_owns_is_never_offered_for_deletion(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)  # the user moved it away
    mine = _prefix(tmp_path / "home" / ".wine")
    (mine / "drive_c" / "MyGame").mkdir()
    relink.apply(profile, relink.plan_relink(profile, mine))
    assert not _trees(tmp_path, profile)["prefix"].safe


def test_a_hand_relink_keeps_what_is_still_where_it_was_made(tmp_path):
    profile, prefix, install = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    relink.apply(profile, relink.plan_relink(profile, new))
    assert "install_dir" in profile["owned_trees"]  # the install folder did not move
    assert profile["owned_paths"]["install_dir"] == str(install.resolve())
    assert profile["owned_dirs"]  # the wrapper Nadare made is still its own


def test_nadares_own_move_still_transfers_ownership(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new, made_by_nadare=True)
    relink.apply(profile, plan)
    assert "prefix" in profile["owned_trees"]
    assert profile["owned_paths"]["prefix"] == str(new.resolve())


def test_the_plan_says_the_folder_will_be_kept(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new)
    assert any("will not delete" in line for line in plan.notes)
    own = relink.plan_relink(profile, new, made_by_nadare=True)
    assert not any("will not delete" in line for line in own.notes)


# ---- stored paths ------------------------------------------------------------------------------
def test_a_relative_pick_is_stored_absolute_and_clean(tmp_path, monkeypatch):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)
    _prefix(tmp_path / "rel" / "pfx")
    monkeypatch.chdir(tmp_path)
    plan = relink.plan_relink(profile, "rel/other/../pfx")
    assert plan.changes["prefix"] == str(tmp_path / "rel" / "pfx")


def test_a_tilde_is_expanded(tmp_path, monkeypatch):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    _prefix(tmp_path / "h" / "moved")
    plan = relink.plan_relink(profile, "~/moved")
    assert plan.changes["prefix"] == str(tmp_path / "h" / "moved")


def test_installed_prefix_follows_a_symlink_spelling_of_the_old_prefix(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    link = tmp_path / "linkto"
    os.symlink(prefix, link)
    profile["installed_prefix"] = str(link)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new, made_by_nadare=True)
    assert plan.changes.get("installed_prefix") == str(new)


def test_a_different_installed_prefix_is_left_alone(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    profile["installed_prefix"] = str(tmp_path / "somewhere-else")
    new = _moved_by_hand(profile, prefix, tmp_path)
    assert "installed_prefix" not in relink.plan_relink(profile, new).changes


# ---- another game's prefix and a game folder left behind -----------------------------------------
def test_a_prefix_another_game_uses_is_flagged_not_refused(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)
    shared = _prefix(tmp_path / "shared" / "pfx")
    others = {"game": profile, "other": {"title": "Other Game", "prefix": str(shared)}}
    plan = relink.plan_relink(profile, shared, name="game", profiles=others)
    assert plan.changes["prefix"] == str(shared)
    assert any("Other Game" in line and "also used" in line for line in plan.warnings)


def test_a_prefix_nobody_else_uses_has_no_warning(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    others = {"game": profile, "other": {"title": "Other", "prefix": str(tmp_path / "unrelated")}}
    plan = relink.plan_relink(profile, new, name="game", profiles=others)
    assert plan.warnings == [] or all("also used" not in w for w in plan.warnings)


def test_an_outside_game_folder_left_behind_is_reported(tmp_path):
    profile, prefix, install = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new)
    assert "install_dir" not in plan.changes
    assert any(str(install) in line for line in plan.warnings)
    assert plan.sibling_install is None


def test_a_game_folder_next_to_the_new_prefix_is_offered(tmp_path):
    profile, prefix, install = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    moved_install = new.parent / "install"
    shutil.move(str(install), str(moved_install))
    plan = relink.plan_relink(profile, new)
    assert plan.sibling_install == str(moved_install)
    assert "install_dir" not in plan.changes  # offered, never applied silently


def test_a_game_folder_inside_the_prefix_still_follows_the_move(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    inside = prefix / "drive_c" / "Games" / "Game"
    inside.mkdir(parents=True)
    profile["install_dir"] = str(inside)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new)
    assert plan.changes["install_dir"] == str(new / "drive_c" / "Games" / "Game")
    assert plan.warnings == [] and plan.sibling_install is None


def test_a_profile_without_any_ownership_records_is_unaffected(tmp_path):
    new = _prefix(tmp_path / "new")
    profile = {"backend": "wine", "prefix": str(tmp_path / "gone")}
    plan = relink.plan_relink(profile, new)
    relink.apply(profile, plan)
    assert profile["prefix"] == str(new)
    assert "owned_trees" not in profile or "prefix" not in profile["owned_trees"]


@pytest.mark.parametrize("bad", ["", "/nonexistent-nadare-test-folder"])
def test_a_folder_that_is_no_prefix_is_still_refused(tmp_path, bad):
    profile, _, _ = _profile(tmp_path)
    with pytest.raises(relink.RelinkError):
        relink.plan_relink(profile, bad or tmp_path)


def test_following_the_offered_game_folder_applies_it_and_drops_its_ownership(tmp_path):
    profile, prefix, install = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    moved_install = new.parent / "install"
    shutil.move(str(install), str(moved_install))
    plan = relink.plan_relink(profile, new)
    followed = relink.follow_install(profile, plan, plan.sibling_install)
    assert followed.sibling_install is None
    relink.apply(profile, followed)
    assert profile["install_dir"] == str(moved_install)
    assert "install_dir" not in profile["owned_trees"]
    assert "install_dir" not in profile["owned_paths"]
    trees = _trees(tmp_path, profile)
    assert not trees["install_dir"].safe and not trees["prefix"].safe


def test_following_a_game_folder_does_not_touch_a_profile_without_ownership(tmp_path):
    plan = relink.Relink(changes={"prefix": "/new"})
    followed = relink.follow_install({"backend": "wine"}, plan, "/new-install")
    assert followed.changes == {"prefix": "/new", "install_dir": "/new-install"}


# ---- review findings (scrutiny s1) -------------------------------------------------------------
def test_a_hand_relink_never_passes_installed_prefix_on(tmp_path):
    """``installed_prefix`` is the backend's proof of what it installed; a pick is no proof."""
    profile, prefix, _ = _profile(tmp_path)
    new = _moved_by_hand(profile, prefix, tmp_path)
    plan = relink.plan_relink(profile, new)
    assert "installed_prefix" not in plan.changes
    relink.apply(profile, plan)
    assert profile["installed_prefix"] == str(prefix)  # still the place Nadare installed into


def _legacy(tmp_path: Path) -> tuple[dict, Path]:
    """A profile from before paths were recorded: ``owned_trees`` but no ``owned_paths``."""
    prefix = _prefix(tmp_path / "old" / "pfx")
    return {"backend": "wine", "prefix": str(prefix), "owned_trees": ["prefix"]}, prefix


def test_a_legacy_profile_relinked_to_a_copy_does_not_own_the_copy(tmp_path):
    profile, _old = _legacy(tmp_path)  # the old folder still exists: the user copied it
    mine = _prefix(tmp_path / "user" / "wine-home")
    (mine / "drive_c" / "save.dat").write_text("precious")
    relink.apply(profile, relink.plan_relink(profile, mine))
    assert "prefix" not in profile.get("owned_trees", [])
    assert not _trees(tmp_path, profile)["prefix"].safe


def test_a_legacy_profile_whose_old_prefix_is_gone_does_not_own_the_pick(tmp_path):
    profile, old = _legacy(tmp_path)
    shutil.rmtree(old)
    mine = _prefix(tmp_path / "user" / "wine-home")
    relink.apply(profile, relink.plan_relink(profile, mine))
    assert not _trees(tmp_path, profile)["prefix"].safe


def test_an_old_prefix_kept_only_under_extra_still_counts_as_the_old_prefix(tmp_path):
    old = _prefix(tmp_path / "old" / "pfx")
    profile = {"backend": "wine", "extra": {"prefix": str(old)}, "owned_trees": ["prefix"]}
    mine = _prefix(tmp_path / "user" / "wine-home")
    plan = relink.plan_relink(profile, mine)
    assert plan.changes["owned_trees"] == []
    relink.apply(profile, plan)
    assert profile["prefix"] == str(mine) and not _trees(tmp_path, profile)["prefix"].safe


def test_pointing_at_the_prefix_kept_under_extra_is_refused_as_unchanged(tmp_path):
    old = _prefix(tmp_path / "old" / "pfx")
    profile = {"backend": "wine", "extra": {"prefix": str(old)}}
    with pytest.raises(relink.RelinkError, match="already"):
        relink.plan_relink(profile, old)


def test_a_prefix_another_game_keeps_under_extra_or_wineprefix_is_flagged(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)
    shared = _prefix(tmp_path / "shared" / "pfx")
    others = {
        "game": profile,
        "viaextra": {"title": "Via Extra", "extra": {"prefix": str(shared)}},
    }
    plan = relink.plan_relink(profile, shared, name="game", profiles=others)
    assert any("Via Extra" in line for line in plan.warnings)
    others = {"game": profile, "alias": {"title": "Alias", "wineprefix": str(shared)}}
    plan = relink.plan_relink(profile, shared, name="game", profiles=others)
    assert any("Alias" in line for line in plan.warnings)


def test_dotdot_through_a_symlink_means_the_real_folder(tmp_path):
    profile, prefix, _ = _profile(tmp_path)
    shutil.rmtree(prefix)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "work" / "link"
    link.parent.mkdir()
    os.symlink(real, link)
    meant = _prefix(tmp_path / "pfx")  # link/.. is tmp_path for the kernel, tmp_path/work lexically
    plan = relink.plan_relink(profile, f"{link}/../pfx")
    assert Path(plan.changes["prefix"]).resolve() == meant.resolve()
