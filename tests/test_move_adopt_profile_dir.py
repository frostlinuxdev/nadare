# SPDX-License-Identifier: GPL-3.0-or-later
"""Move converts a game Nadare kept in its own data folder into ``<library>/Nadare/<Game>``.

Older installs live in ``<data>/profiles/<id>/{prefix,install}``. Move used to treat those as a
bare prefix and drop it at ``<picked>/prefix``; now the whole folder moves as one unit and gets
the layout Add Game makes. A game whose ``install`` folder does not exist yet (NIKKE installs
through its own launcher) moves too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import paths, relink, relocate


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data" / "profiles"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    return root


def _old(data: Path, ident: str = "nikke", *, install: bool = True, title: str = "NIKKE") -> dict:
    home = data / ident
    prefix = home / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    (prefix / "system.reg").write_text("reg")
    profile = {
        "backend": "nikke",
        "title": title,
        "prefix": str(prefix),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }
    if install:
        (home / "install").mkdir()
        (home / "install" / "game.exe").write_bytes(b"MZ" * 50)
    return profile


def test_a_game_in_nadares_profiles_folder_is_a_managed_home(data):
    profile = _old(data)
    assert relink.managed_home(profile) == data / "nikke"


def test_a_prefix_outside_the_profiles_folder_is_not_inferred(data, tmp_path):
    profile = _old(tmp_path / "elsewhere")
    assert relink.managed_home(profile) is None


def test_a_prefix_nadare_did_not_make_is_not_inferred(data):
    profile = _old(data)
    profile["owned_trees"] = ["install_dir"]
    assert relink.managed_home(profile) is None


def test_a_missing_game_folder_does_not_block_the_move(data):
    profile = _old(data, install=False)
    assert relink.managed_home(profile) == data / "nikke"


def test_the_new_folder_is_named_after_the_game(data):
    profile = _old(data)
    home = relink.managed_home(profile)
    assert relink.home_name(profile, home) == "NIKKE"


def test_an_add_game_folder_keeps_its_own_name(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Some Game"
    assert relink.home_name({"title": "Other"}, home) == "Some Game"


def test_a_title_cannot_escape_the_library(data):
    profile = _old(data, title="../evil/name")
    name = relink.home_name(profile, data / "nikke")
    assert "/" not in name and name not in ("", ".", "..")


def test_the_whole_profile_folder_moves_into_the_library_layout(data, tmp_path):
    profile = _old(data)
    home = relink.managed_home(profile)
    library = tmp_path / "izuna"
    library.mkdir()
    new_home = relink.free_home(library, relink.home_name(profile, home), {})
    relocate.move_tree(home, new_home)
    relink.apply(profile, relink.plan_home_move(profile, new_home))
    assert new_home == library / "Nadare" / "NIKKE"
    assert profile["prefix"] == str(new_home / "prefix")
    assert profile["install_dir"] == str(new_home / "install")
    assert profile["owned_dirs"] == [str(new_home)]
    assert relink.managed_home(profile) == new_home


def test_a_missing_game_folder_stays_missing_and_is_re_rooted(data, tmp_path):
    profile = _old(data, install=False)
    home = relink.managed_home(profile)
    new_home = tmp_path / "izuna" / "Nadare" / "NIKKE"
    relocate.move_tree(home, new_home)
    plan = relink.plan_home_move(profile, new_home, install_existed=False)
    relink.apply(profile, plan)
    assert profile["install_dir"] == str(new_home / "install")
    assert not (new_home / "install").exists()


def test_a_game_folder_that_existed_must_arrive(data, tmp_path):
    profile = _old(data)
    home = relink.managed_home(profile)
    new_home = tmp_path / "izuna" / "Nadare" / "NIKKE"
    relocate.move_tree(home, new_home)
    (new_home / "install" / "game.exe").unlink()
    (new_home / "install").rmdir()
    with pytest.raises(relink.RelinkError, match="game folder did not arrive"):
        relink.plan_home_move(profile, new_home)


# ---- a prefix-only move names the folder after the game ----------------------------------------
def test_a_generic_prefix_folder_is_named_after_the_game(tmp_path):
    old = tmp_path / "x" / "prefix"
    dst = relink.prefix_destination({"title": "NIKKE"}, old, tmp_path / "izuna")
    assert dst == tmp_path / "izuna" / "NIKKE prefix"


def test_a_meaningful_prefix_folder_name_is_kept(tmp_path):
    old = tmp_path / "testing" / "CZN"
    assert relink.prefix_destination({"title": "CZN"}, old, tmp_path / "izuna") == (
        tmp_path / "izuna" / "CZN"
    )


def test_a_taken_prefix_destination_gets_a_number(tmp_path):
    old = tmp_path / "x" / "prefix"
    lib = tmp_path / "izuna"
    (lib / "NIKKE prefix").mkdir(parents=True)
    assert relink.prefix_destination({"title": "NIKKE"}, old, lib) == lib / "NIKKE prefix 2"


def test_a_prefix_destination_title_cannot_escape_the_folder(tmp_path):
    old = tmp_path / "x" / "prefix"
    dst = relink.prefix_destination({"title": "../../evil"}, old, tmp_path / "izuna")
    assert dst.parent == tmp_path / "izuna"


# ---- scrutiny: only a folder holding nothing but the game and its prefix moves as a unit --------
def test_backend_state_beside_the_prefix_keeps_the_game_prefix_only(data):
    profile = _old(data, install=False)
    (data / "nikke" / "auth").mkdir()
    (data / "nikke" / "auth" / "state.json").write_text("{}")
    assert relink.managed_home(profile) is None


def test_stray_files_beside_the_prefix_keep_the_game_prefix_only(data):
    profile = _old(data)
    (data / "nikke" / "notes.txt").write_text("x")
    assert relink.managed_home(profile) is None


def test_a_path_in_extra_inside_the_folder_keeps_the_game_prefix_only(data):
    profile = _old(data)
    profile["extra"] = {"script": str(data / "nikke" / "install" / "run.sh")}
    assert relink.managed_home(profile) is None


def test_an_absolute_exe_inside_the_folder_keeps_the_game_prefix_only(data):
    profile = _old(data)
    profile["exe_name"] = str(data / "nikke" / "install" / "game.exe")
    assert relink.managed_home(profile) is None


def test_the_recorded_keys_alone_do_not_block_it(data):
    profile = _old(data)
    profile["installed_prefix"] = profile["prefix"]
    profile["prefix_backups"] = [str(data / "nikke" / "prefix.bak")]
    assert relink.managed_home(profile) == data / "nikke"
