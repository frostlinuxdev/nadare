# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: a catalog game's folders are offered for deletion only inside its own game folder."""

from __future__ import annotations

from pathlib import Path

from nadare.core import libraries, removal
from nadare.core.config import Config


def _library_game(tmp_path) -> tuple[Config, Path]:
    config = Config.load(tmp_path / "config.json")
    lib = tmp_path / "Games"
    libraries.add_library(config, lib)
    game = lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / "prefix" / "drive_c").mkdir(parents=True)
    config.add_profile(
        "bd2", backend="direct-bd2", title="Brown Dust 2",
        install_dir=str(game / "install"), prefix=str(game / "prefix"),
        owned_dirs=[str(game)], owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str((game / "install").resolve()),
                     "prefix": str((game / "prefix").resolve())},
    )
    return config, game


def _trees(config, name="bd2"):
    return {t.key: t for t in removal.removable_trees(config, name)}


def test_a_library_games_own_folders_are_deletable(tmp_path):
    config, _ = _library_game(tmp_path)
    trees = _trees(config)
    assert trees["install_dir"].safe and trees["prefix"].safe


def test_an_owned_record_pointing_outside_the_game_folder_is_kept(tmp_path):
    config, game = _library_game(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # An old record that claims a folder outside the game folder (e.g. a hand-edited config).
    profile = config.profiles["bd2"]
    profile["install_dir"] = str(elsewhere)
    profile["owned_paths"]["install_dir"] = str(elsewhere.resolve())
    trees = _trees(config)
    assert not trees["install_dir"].safe
    assert "outside" in trees["install_dir"].reason
    assert trees["prefix"].safe


def test_a_custom_wine_game_keeps_todays_rule(tmp_path):
    config = Config.load(tmp_path / "config.json")
    folder = tmp_path / "anywhere" / "pfx"
    (folder / "drive_c").mkdir(parents=True)
    config.add_profile(
        "w", backend="wine", title="W", prefix=str(folder),
        owned_trees=["prefix"], owned_paths={"prefix": str(folder.resolve())},
    )
    assert _trees(config, "w")["prefix"].safe


def test_a_default_library_game_is_unchanged(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2")
    from nadare.core import paths

    (paths.profiles_dir() / "bd2" / "prefix" / "drive_c").mkdir(parents=True)
    assert _trees(config)["prefix"].safe


def test_an_older_profile_without_a_recorded_game_folder_keeps_todays_rule(tmp_path):
    config = Config.load(tmp_path / "config.json")
    game = tmp_path / "Games" / "Nadare" / "NIKKE"
    (game / "drive_c").mkdir(parents=True)
    config.add_profile(
        "nikke", backend="launcher-nikke", title="NIKKE", prefix=str(game),
        owned_trees=["prefix"],
    )
    assert _trees(config, "nikke")["prefix"].safe


def test_a_recorded_wrapper_outside_the_library_layout_keeps_todays_rule(tmp_path):
    config = Config.load(tmp_path / "config.json")
    wrapper = tmp_path / "MyStuff" / "BD2"
    (wrapper / "install").mkdir(parents=True)
    (wrapper / "prefix" / "drive_c").mkdir(parents=True)
    config.add_profile(
        "bd2", backend="direct-bd2", title="Brown Dust 2",
        install_dir=str(wrapper / "install"), prefix=str(wrapper / "prefix"),
        owned_dirs=[str(wrapper)], owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str((wrapper / "install").resolve()),
                     "prefix": str((wrapper / "prefix").resolve())},
    )
    trees = _trees(config)
    assert trees["install_dir"].safe and trees["prefix"].safe
