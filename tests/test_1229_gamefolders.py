# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: where each game's folder is, whether it is in a library, and what points elsewhere."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import gamefolders, libraries, paths
from nadare.core.config import Config


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config.load(tmp_path / "config.json")


def _library_game(config, lib: Path, title: str = "Brown Dust 2", kind: str = "direct-bd2"):
    libraries.add_library(config, lib)
    game = libraries.default_root() if lib is None else lib / libraries.NADARE_DIR / title
    install, prefix = game / "install", game / "prefix"
    install.mkdir(parents=True)
    (prefix / "drive_c").mkdir(parents=True)
    config.add_profile(
        "bd2",
        backend=kind,
        title=title,
        install_dir=str(install),
        prefix=str(prefix),
        owned_dirs=[str(game)],
        owned_trees=["install_dir", "prefix"],
    )
    libraries.write_manifest(config.profiles["bd2"], game)
    return game


def test_a_library_game_is_in_its_library(config, tmp_path):
    lib = tmp_path / "Games"
    game = _library_game(config, lib)
    place = gamefolders.placement(config, "bd2")
    assert place.folder == game.resolve()
    assert place.library == lib.resolve()
    assert place.made_by_nadare
    assert place.stray == {}


def test_a_default_library_game(config):
    config.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2")
    place = gamefolders.placement(config, "bd2")
    assert place.folder == (paths.profiles_dir() / "bd2").resolve()
    assert place.library == libraries.default_root()


def test_an_import_is_outside_the_libraries_and_not_nadares(config, tmp_path):
    where = tmp_path / "old-games" / "BD2"
    where.mkdir(parents=True)
    config.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2", install_dir=str(where))
    place = gamefolders.placement(config, "bd2")
    assert place.folder == where.resolve()
    assert place.library is None
    assert not place.made_by_nadare
    assert place.stray == {}


def test_an_import_that_sits_in_a_library_counts_as_in_it(config, tmp_path):
    lib = tmp_path / "Games"
    libraries.add_library(config, lib)
    where = lib / libraries.NADARE_DIR / "BD2"
    where.mkdir(parents=True)
    config.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2", install_dir=str(where))
    place = gamefolders.placement(config, "bd2")
    assert place.library == lib.resolve()
    assert not place.made_by_nadare


def test_a_settings_path_pointing_elsewhere_is_stray_with_its_reset(config, tmp_path):
    lib = tmp_path / "Games"
    game = _library_game(config, lib)
    config.profiles["bd2"]["install_dir"] = str(tmp_path / "somewhere-else")
    place = gamefolders.placement(config, "bd2")
    assert place.folder == game.resolve()
    assert place.stray == {"install_dir": str((game / "install").resolve())}


def test_a_launcher_games_install_folder_is_stray_and_resets_to_nothing(config, tmp_path):
    lib = tmp_path / "Games"
    libraries.add_library(config, lib)
    game = lib / libraries.NADARE_DIR / "NIKKE"
    (game / "prefix" / "drive_c").mkdir(parents=True)
    config.add_profile(
        "nikke",
        backend="launcher-nikke",
        title="NIKKE",
        prefix=str(game / "prefix"),
        install_dir=str(tmp_path / "src" / "nadare"),
        owned_dirs=[str(game)],
        owned_trees=["prefix"],
    )
    place = gamefolders.placement(config, "nikke")
    assert place.folder == game.resolve()
    assert place.stray == {"install_dir": None}


def test_reset_puts_the_stray_values_back(config, tmp_path):
    lib = tmp_path / "Games"
    game = _library_game(config, lib)
    config.profiles["bd2"]["install_dir"] = str(tmp_path / "somewhere-else")
    assert gamefolders.reset_stray(config, "bd2") == ["install_dir"]
    assert config.profiles["bd2"]["install_dir"] == str((game / "install").resolve())
    assert gamefolders.placement(config, "bd2").stray == {}


def test_reset_drops_a_launcher_games_install_folder(config, tmp_path):
    lib = tmp_path / "Games"
    libraries.add_library(config, lib)
    game = lib / libraries.NADARE_DIR / "NIKKE"
    (game / "prefix" / "drive_c").mkdir(parents=True)
    config.add_profile(
        "nikke",
        backend="launcher-nikke",
        title="NIKKE",
        prefix=str(game / "prefix"),
        install_dir="/elsewhere",
        owned_dirs=[str(game)],
        owned_trees=["prefix"],
    )
    assert gamefolders.reset_stray(config, "nikke") == ["install_dir"]
    assert "install_dir" not in config.profiles["nikke"]


@pytest.mark.parametrize("kind", ["wine", "steam", "script"])
def test_custom_and_steam_games_are_outside_the_model(config, kind):
    config.add_profile("x", backend=kind, title="X")
    assert gamefolders.placement(config, "x") is None


def test_nothing_is_moved_or_written_by_looking(config, tmp_path):
    lib = tmp_path / "Games"
    game = _library_game(config, lib)
    config.profiles["bd2"]["install_dir"] = str(tmp_path / "somewhere-else")
    before = dict(config.profiles["bd2"])
    listing = sorted(p.name for p in game.iterdir())
    gamefolders.placement(config, "bd2")
    assert config.profiles["bd2"] == before
    assert sorted(p.name for p in game.iterdir()) == listing
    assert not (tmp_path / "somewhere-else").exists()
