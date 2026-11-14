# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade: a profile made before ``owned_dirs`` existed (before 2026-10-27, b17 and older).

Add Game always laid a game out as ``<library>/Nadare/<Game>/{prefix,install}`` and recorded
``owned_trees``, but not yet the folder itself. ``relink`` already recognises that layout; the
library code did not, so Settings placed such a game in ``profiles/<name>`` (wrong library, a
"settings point outside the game folder" warning) and its manifest was never written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import gamefolders, libraries
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), (tmp_path / "Games").resolve()


def _legacy(cfg, game: Path, *, owned=("install_dir", "prefix"), install=True):
    (game / "prefix" / "drive_c").mkdir(parents=True)
    values = {"backend": "direct-bd2", "title": "Brown Dust 2", "prefix": str(game / "prefix")}
    if install:
        (game / "install").mkdir()
        values["install_dir"] = str(game / "install")
    if owned is not None:
        values["owned_trees"] = list(owned)
    cfg.add_profile("bd2", **values)


def test_an_old_profile_in_a_library_is_placed_in_its_game_folder(env):
    cfg, lib = env
    game = lib / "Nadare" / "Brown Dust 2"
    _legacy(cfg, game)
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib)}]
    place = gamefolders.placement(cfg, "bd2")
    assert place is not None
    assert place.folder == game
    assert place.made_by_nadare
    assert place.library == lib
    assert not place.stray


def test_the_next_start_writes_its_manifest_and_remembers_the_library(env):
    cfg, lib = env
    game = lib / "Nadare" / "Brown Dust 2"
    _legacy(cfg, game)
    libraries.backfill(cfg)
    manifest = libraries.read_manifest(game)
    assert manifest is not None and manifest.kind == "direct-bd2"
    assert lib in libraries.library_roots(cfg)


def test_an_old_profile_that_owns_nothing_is_not_claimed(env):
    """No ownership record: the user pointed Nadare at those folders; never claim them."""
    cfg, lib = env
    game = lib / "Nadare" / "Brown Dust 2"
    _legacy(cfg, game, owned=None)
    place = gamefolders.placement(cfg, "bd2")
    assert place is None or not place.made_by_nadare
    libraries.backfill(cfg)
    assert not (game / libraries.MANIFEST_NAME).exists()


def test_a_layout_that_no_longer_matches_is_not_claimed(env):
    """The install folder was pointed elsewhere since: not the layout Add Game made."""
    cfg, lib = env
    game = lib / "Nadare" / "Brown Dust 2"
    _legacy(cfg, game)
    elsewhere = lib / "elsewhere"
    elsewhere.mkdir()
    cfg.profiles["bd2"]["install_dir"] = str(elsewhere)
    libraries.backfill(cfg)
    assert not (game / libraries.MANIFEST_NAME).exists()


def test_a_prefix_only_old_profile_is_placed_too(env):
    cfg, lib = env
    game = lib / "Nadare" / "NIKKE"
    (game / "prefix" / "drive_c").mkdir(parents=True)
    cfg.add_profile(
        "nikke", backend="launcher-nikke", title="NIKKE",
        prefix=str(game / "prefix"), owned_trees=["prefix"],
    )
    place = gamefolders.placement(cfg, "nikke")
    assert place is not None and place.folder == game and place.made_by_nadare


def test_a_default_library_game_is_unchanged(env):
    cfg, _lib = env
    cfg.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2")
    place = gamefolders.placement(cfg, "bd2")
    assert place is None or place.folder == libraries.default_root() / "bd2"
