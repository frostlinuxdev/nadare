# SPDX-License-Identifier: GPL-3.0-or-later
"""Moving a game keeps the layout Add Game made: ``<library>/Nadare/<Game>/{install,prefix}``.

The old Move put only the prefix at ``<picked>/prefix``: the sibling game folder stayed behind with
``install_dir`` unchanged, and two games could not share a destination (move/prefix analysis).
Now a game Nadare made moves as one unit to ``<picked library>/Nadare/<Game>``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from nadare.core import relink, relocate


def _make(lib: Path, name: str = "Game") -> dict:
    """The profile Add Game makes, with its folders on disk (as after an install)."""
    home = lib / "Nadare" / name
    prefix = home / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    (prefix / "system.reg").write_text("reg")
    install = home / "install"
    install.mkdir()
    (install / "game.exe").write_bytes(b"MZ" * 50)
    return {
        "backend": "arknights-yostar",
        "title": name,
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


def _move(profile: dict, library: Path, profiles: dict | None = None) -> Path:
    """What the Settings page does: pick a library, move the home, re-root the profile."""
    home = relink.managed_home(profile)
    assert home is not None
    new_home = relink.free_home(library, home.name, profiles or {})
    relocate.move_tree(home, new_home)
    relink.apply(profile, relink.plan_home_move(profile, new_home))
    return new_home


# ---- is this a layout Nadare made? -------------------------------------------------------------
def test_the_add_game_layout_is_a_managed_home(tmp_path):
    profile = _make(tmp_path / "lib")
    assert relink.managed_home(profile) == tmp_path / "lib" / "Nadare" / "Game"


def test_a_legacy_profile_without_owned_dirs_is_still_recognised(tmp_path):
    # Older Nadare recorded only owned_trees/owned_paths for the same Nadare/<Game> layout.
    profile = _make(tmp_path / "lib")
    del profile["owned_dirs"]
    assert relink.managed_home(profile) == tmp_path / "lib" / "Nadare" / "Game"


@pytest.mark.parametrize("prefix_name,install_name", [("pfx", "install"), ("prefix", "game")])
def test_a_folder_not_named_like_add_game_names_is_not_inferred(
    tmp_path, prefix_name, install_name
):
    profile = _make(tmp_path / "lib")
    del profile["owned_dirs"]
    home = tmp_path / "lib" / "Nadare" / "Game"
    (home / "prefix").rename(home / prefix_name)
    (home / "install").rename(home / install_name)
    profile["prefix"], profile["install_dir"] = str(home / prefix_name), str(home / install_name)
    assert relink.managed_home(profile) is None


def test_a_legacy_layout_outside_a_nadare_folder_is_not_inferred(tmp_path):
    profile = _make(tmp_path / "lib")
    del profile["owned_dirs"]
    home = tmp_path / "lib" / "Nadare" / "Game"
    elsewhere = tmp_path / "lib" / "Mine" / "Game"
    elsewhere.parent.mkdir()
    home.rename(elsewhere)
    profile["prefix"] = str(elsewhere / "prefix")
    profile["install_dir"] = str(elsewhere / "install")
    assert relink.managed_home(profile) is None


def test_a_legacy_move_keeps_the_layout_and_records_the_new_folder(tmp_path):
    profile = _make(tmp_path / "lib")
    del profile["owned_dirs"]
    new_home = _move(profile, tmp_path / "bigdisk")
    assert Path(profile["install_dir"]).parent == Path(profile["prefix"]).parent == new_home
    assert profile["owned_dirs"] == [str(new_home)]


def test_a_prefix_nadare_did_not_create_is_not_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    profile["owned_trees"] = ["install_dir"]
    assert relink.managed_home(profile) is None


def test_a_game_folder_outside_the_home_is_not_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    elsewhere = tmp_path / "games" / "Game"
    elsewhere.mkdir(parents=True)
    profile["install_dir"] = str(elsewhere)
    assert relink.managed_home(profile) is None


def test_a_prefix_only_profile_inside_its_home_is_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    profile["install_dir"] = ""
    profile["owned_trees"] = ["prefix"]
    assert relink.managed_home(profile) == tmp_path / "lib" / "Nadare" / "Game"


def test_a_home_that_is_a_symlink_is_not_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    real = tmp_path / "lib" / "Nadare" / "Game"
    link = tmp_path / "lib" / "Nadare" / "Link"
    os.symlink(real, link)
    profile.update(
        prefix=str(link / "prefix"),
        install_dir=str(link / "install"),
        owned_dirs=[str(link)],
    )
    assert relink.managed_home(profile) is None


# ---- where it goes -----------------------------------------------------------------------------
def test_the_new_home_is_library_nadare_game(tmp_path):
    assert relink.free_home(tmp_path / "big", "Game", {}) == tmp_path / "big" / "Nadare" / "Game"


def test_a_taken_name_gets_a_number_like_add_game(tmp_path):
    (tmp_path / "big" / "Nadare" / "Game").mkdir(parents=True)
    assert relink.free_home(tmp_path / "big", "Game", {}) == (
        tmp_path / "big" / "Nadare" / "Game 2"
    )


def test_a_name_another_profile_claims_is_taken_even_before_it_exists(tmp_path):
    claimed = tmp_path / "big" / "Nadare" / "Game"
    others = {"x": {"owned_dirs": [str(claimed)], "prefix": str(claimed / "prefix")}}
    assert relink.free_home(tmp_path / "big", "Game", others) == (
        tmp_path / "big" / "Nadare" / "Game 2"
    )


def test_a_relative_library_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert relink.free_home("rel", "Game", {}) == tmp_path / "rel" / "Nadare" / "Game"


# ---- the plan ----------------------------------------------------------------------------------
def test_the_plan_re_roots_every_path_under_the_home(tmp_path):
    profile = _make(tmp_path / "lib")
    new_home = tmp_path / "big" / "Nadare" / "Game"
    shutil.copytree(tmp_path / "lib" / "Nadare" / "Game", new_home)
    plan = relink.plan_home_move(profile, new_home)
    assert plan.changes["prefix"] == str(new_home / "prefix")
    assert plan.changes["install_dir"] == str(new_home / "install")
    assert plan.changes["installed_prefix"] == str(new_home / "prefix")
    assert plan.changes["owned_dirs"] == [str(new_home)]
    assert plan.changes["owned_paths"] == {
        "install_dir": str((new_home / "install").resolve()),
        "prefix": str((new_home / "prefix").resolve()),
    }


def test_a_plan_for_a_copy_that_is_missing_files_is_refused(tmp_path):
    profile = _make(tmp_path / "lib")
    new_home = tmp_path / "big" / "Nadare" / "Game"
    (new_home / "install").mkdir(parents=True)  # no prefix came across
    with pytest.raises(relink.RelinkError):
        relink.plan_home_move(profile, new_home)


def test_a_plan_for_an_unmanaged_profile_is_refused(tmp_path):
    profile = _make(tmp_path / "lib")
    del profile["owned_dirs"]
    profile["owned_trees"] = ["install_dir"]  # the prefix is the user's own
    with pytest.raises(relink.RelinkError):
        relink.plan_home_move(profile, tmp_path / "anywhere")


# ---- the whole move ----------------------------------------------------------------------------
def test_a_move_keeps_the_layout_and_leaves_nothing_behind(tmp_path):
    profile = _make(tmp_path / "lib")
    new_home = _move(profile, tmp_path / "bigdisk")
    assert new_home == tmp_path / "bigdisk" / "Nadare" / "Game"
    assert (new_home / "prefix" / "drive_c").is_dir()
    assert (new_home / "install" / "game.exe").is_file()
    assert not (tmp_path / "lib" / "Nadare" / "Game").exists()
    assert Path(profile["prefix"]).is_dir() and Path(profile["install_dir"]).is_dir()
    assert profile["owned_dirs"] == [str(new_home)]


def test_two_games_can_share_one_destination(tmp_path):
    first, second = _make(tmp_path / "lib", "Alpha"), _make(tmp_path / "lib", "Beta")
    profiles = {"a": first, "b": second}
    home_a = _move(first, tmp_path / "big", profiles)
    home_b = _move(second, tmp_path / "big", profiles)
    assert home_a == tmp_path / "big" / "Nadare" / "Alpha"
    assert home_b == tmp_path / "big" / "Nadare" / "Beta"
    for profile in (first, second):
        assert Path(profile["prefix"]).is_dir() and Path(profile["install_dir"]).is_dir()


def test_two_games_with_the_same_folder_name_get_distinct_homes(tmp_path):
    first, second = _make(tmp_path / "one", "Game"), _make(tmp_path / "two", "Game")
    profiles = {"a": first, "b": second}
    home_a = _move(first, tmp_path / "big", profiles)
    home_b = _move(second, tmp_path / "big", profiles)
    assert home_a != home_b and home_b.name == "Game 2"


def test_a_move_to_another_disk_copies_keeps_the_old_one_and_re_roots(tmp_path):
    shm = Path("/dev/shm")
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        pytest.skip("no writable tmpfs for a cross-device move")
    profile = _make(tmp_path / "lib")
    other = Path(tempfile.mkdtemp(prefix="nadare-move-test-", dir=shm))
    try:
        home = relink.managed_home(profile)
        new_home = relink.free_home(other, home.name, {})
        moved = relocate.move_tree(home, new_home)
        assert moved.renamed is False and moved.kept_old == home
        relink.apply(profile, relink.plan_home_move(profile, new_home))
        assert (new_home / "prefix" / "drive_c").is_dir() and (new_home / "install").is_dir()
        assert home.is_dir()  # the original is kept, as the user was told
        assert profile["prefix"] == str(new_home / "prefix")
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_picking_the_library_the_game_already_lives_in_changes_nothing(tmp_path):
    profile = _make(tmp_path / "lib")
    home = relink.managed_home(profile)
    assert relink.free_home(tmp_path / "lib", home.name, {}) != home  # a free name, never the same
    with pytest.raises(relink.RelinkError, match="already"):
        relink.check_home_target(profile, tmp_path / "lib")


# ---- refusals ----------------------------------------------------------------------------------
def test_a_symlinked_prefix_is_refused_up_front_with_a_plain_reason(tmp_path):
    real = tmp_path / "real"
    (real / "drive_c").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(relink.RelinkError, match="link"):
        relink.check_movable({"prefix": str(link)})


# ---- recorded rebuild backups travel with the home --------------------------------------------
def test_recorded_prefix_backups_inside_the_home_are_re_rooted(tmp_path):
    profile = _make(tmp_path / "lib")
    home = tmp_path / "lib" / "Nadare" / "Game"
    backup = home / "prefix.broken-20261029-120000"
    backup.mkdir()
    elsewhere = tmp_path / "other" / "prefix.broken-1"
    profile["prefix_backups"] = [str(backup), str(elsewhere), 7]
    new_home = _move(profile, tmp_path / "big")
    assert profile["prefix_backups"] == [
        str(new_home / "prefix.broken-20261029-120000"),
        str(elsewhere),
        7,
    ]
    assert (new_home / "prefix.broken-20261029-120000").is_dir()


def test_a_profile_without_recorded_backups_gets_none_added(tmp_path):
    profile = _make(tmp_path / "lib")
    _move(profile, tmp_path / "big")
    assert "prefix_backups" not in profile


def test_a_malformed_backup_record_is_left_alone(tmp_path):
    profile = _make(tmp_path / "lib")
    profile["prefix_backups"] = "not-a-list"
    _move(profile, tmp_path / "big")
    assert profile["prefix_backups"] == "not-a-list"


# ---- review findings (scrutiny s2) -------------------------------------------------------------
def test_another_game_kept_inside_this_games_folder_blocks_the_move(tmp_path):
    outer = _make(tmp_path / "lib", "Outer")
    outer_home = tmp_path / "lib" / "Nadare" / "Outer"
    inner = _make(outer_home, "Inner")  # Inner's home is Outer/Nadare/Inner
    inner["title"] = "Inner Game"
    profiles = {"outer": outer, "inner": inner}
    with pytest.raises(relink.RelinkError, match="Inner Game"):
        relink.check_home_target(outer, tmp_path / "big", name="outer", profiles=profiles)
    # moving the inner one out is fine: it holds nothing of the outer game's
    relink.check_home_target(inner, tmp_path / "big", name="inner", profiles=profiles)


def test_a_library_inside_another_games_folder_is_refused(tmp_path):
    first = _make(tmp_path / "lib", "First")
    second = _make(tmp_path / "other", "Second")
    second["title"] = "Second Game"
    inside = tmp_path / "other" / "Nadare" / "Second" / "install" / "sub"
    inside.mkdir()
    profiles = {"first": first, "second": second}
    with pytest.raises(relink.RelinkError, match="Second Game"):
        relink.check_home_target(first, inside, name="first", profiles=profiles)


def test_without_the_other_profiles_nothing_extra_is_checked(tmp_path):
    outer = _make(tmp_path / "lib", "Outer")
    _make(tmp_path / "lib" / "Nadare" / "Outer", "Inner")
    relink.check_home_target(outer, tmp_path / "big")  # old call shape still works


def test_a_missing_game_folder_means_the_layout_is_not_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    shutil.rmtree(tmp_path / "lib" / "Nadare" / "Game" / "install")
    assert relink.managed_home(profile) is None
    # after a move the old folder is gone and only the records are read: still managed then
    assert relink.managed_home(profile, exists=False) is not None


def test_a_missing_prefix_means_the_layout_is_not_managed(tmp_path):
    profile = _make(tmp_path / "lib")
    shutil.rmtree(tmp_path / "lib" / "Nadare" / "Game" / "prefix")
    assert relink.managed_home(profile) is None


def test_a_name_claimed_through_another_spelling_is_taken(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claimed = {"x": {"owned_dirs": ["lib/Nadare/Game"], "prefix": "lib/Nadare/Game/prefix"}}
    assert relink.free_home(tmp_path / "lib", "Game", claimed) == (
        tmp_path / "lib" / "Nadare" / "Game 2"
    )
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "alias")
    claimed = {"x": {"owned_dirs": [str(tmp_path / "alias" / "Nadare" / "Game")]}}
    assert relink.free_home(real, "Game", claimed) == real / "Nadare" / "Game 2"


def test_dotdot_in_a_library_means_where_the_kernel_goes(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "work" / "link"
    link.parent.mkdir()
    os.symlink(real, link)
    assert relink.free_home(f"{link}/../big", "Game", {}) == tmp_path / "big" / "Nadare" / "Game"
