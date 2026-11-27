# SPDX-License-Identifier: GPL-3.0-or-later
"""#1362: the b22 game-folder manifest must not leave folders behind or block anything.

Seen on an NVIDIA PC (2026-11-14): ``profiles/brown-dust-2/`` held only ``nadare-game.json`` for a
game that was never installed; Aether Gazer, which Nadare installed in
``profiles/aether-gazer/{prefix,install}``, had no ``owned_trees`` and so could only move its
prefix and was described as "an existing install".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import gamefolders, libraries, paths, relink, removal
from nadare.core.config import Config

KIND = "direct-bd2"
TMP_NAME = f".{libraries.MANIFEST_NAME}.tmp"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home


def _profile_dir_game(cfg: Config, name: str = "bd2", *, owned: bool = True) -> Path:
    """A game in the default library, laid out as b22..b24 made it."""
    home = paths.profiles_dir() / name
    (home / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (home / "prefix" / "system.reg").write_text("reg")
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ" * 10)
    values = {
        "backend": KIND,
        "title": "Brown Dust 2",
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
    }
    if owned:
        values["owned_trees"] = ["install_dir", "prefix"]
    cfg.add_profile(name, **values)
    cfg.save()
    libraries.record_new_game(values, name)
    assert (home / libraries.MANIFEST_NAME).is_file()
    return home


# ---- 1: uninstall tidies the default library's game folder --------------------------------
@pytest.mark.parametrize("delete_data", [True, False])
@pytest.mark.parametrize("owned", [True, False])
def test_uninstalling_from_the_default_library_leaves_no_manifest_only_folder(
    env, tmp_path, owned, delete_data
):
    cfg, _home = env
    home = _profile_dir_game(cfg, owned=owned)
    removal.remove_game(
        cfg,
        "bd2",
        delete_data=delete_data,
        delete_trees=["install_dir", "prefix"],
        applications_dir=tmp_path / "apps",
    )
    assert not home.exists()
    assert paths.profiles_dir().is_dir()  # the default library itself stays


def test_uninstall_keeps_the_folder_when_the_player_kept_the_game_files(env, tmp_path):
    cfg, _home = env
    home = _profile_dir_game(cfg)
    removal.remove_game(cfg, "bd2", applications_dir=tmp_path / "apps")
    assert (home / "install" / "game.exe").is_file()
    assert (home / libraries.MANIFEST_NAME).is_file()  # still findable


# ---- 2 and 5: an interrupted manifest write ------------------------------------------------
def test_a_leftover_manifest_temp_file_does_not_keep_the_folder(tmp_path):
    folder = tmp_path / "Nadare" / "Brown Dust 2"
    folder.mkdir(parents=True)
    (folder / libraries.MANIFEST_NAME).write_text("{}")
    (folder / TMP_NAME).write_text("{")
    removal._drop_lone_manifest(folder)
    assert list(folder.iterdir()) == []


def test_a_user_file_still_keeps_the_manifest(tmp_path):
    folder = tmp_path / "Nadare" / "Brown Dust 2"
    folder.mkdir(parents=True)
    (folder / libraries.MANIFEST_NAME).write_text("{}")
    (folder / "notes.txt").write_text("mine")
    removal._drop_lone_manifest(folder)
    assert (folder / libraries.MANIFEST_NAME).is_file()


def test_a_leftover_manifest_temp_file_does_not_block_the_whole_move(env):
    cfg, _home = env
    home = _profile_dir_game(cfg)
    (home / TMP_NAME).write_text("{")
    assert relink.managed_home(cfg.profiles["bd2"]) == home


# ---- 3: ownership for b22..b24 games in profiles/<id>/ -------------------------------------
def _aether_gazer_b24(cfg: Config) -> Path:
    """The exact shape on an NVIDIA PC: no owned_trees, no owned_dirs, folders made by Nadare."""
    home = paths.profiles_dir() / "aether-gazer"
    (home / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (home / "prefix" / "system.reg").write_text("reg")
    (home / "install").mkdir()
    (home / "install" / "AetherGazer.exe").write_bytes(b"MZ")
    cfg.add_profile(
        "aether-gazer",
        backend="direct-aethergazer",
        title="Aether Gazer",
        prefix=str(home / "prefix"),
        install_dir=str(home / "install"),
    )
    cfg.save()
    return home


def test_startup_records_ownership_for_a_game_in_its_own_profiles_folder(env):
    cfg, _home = env
    home = _aether_gazer_b24(cfg)
    before = [(t.key, str(t.path)) for t in removal.removable_trees(cfg, "aether-gazer")]
    assert libraries.backfill(cfg) is True
    profile = cfg.profiles["aether-gazer"]
    assert sorted(profile["owned_trees"]) == ["install_dir", "prefix"]
    assert relink.managed_home(profile) == home
    assert gamefolders.placement(cfg, "aether-gazer").made_by_nadare
    # Uninstall offers exactly what it offered before: nothing new becomes deletable.
    after = [(t.key, str(t.path)) for t in removal.removable_trees(cfg, "aether-gazer")]
    assert after == before
    assert libraries.backfill(cfg) is False  # once


def test_the_migration_survives_a_save_and_a_reload(env, tmp_path):
    cfg, _home = env
    _aether_gazer_b24(cfg)
    libraries.backfill(cfg)
    cfg.save()
    again = Config.load(tmp_path / "config.json")
    assert sorted(again.profiles["aether-gazer"]["owned_trees"]) == ["install_dir", "prefix"]


@pytest.mark.parametrize(
    "where",
    ["elsewhere", "other_profiles_folder", "install_outside", "already_recorded"],
)
def test_ownership_is_not_guessed_for_other_layouts(env, tmp_path, where):
    cfg, _home = env
    home = _aether_gazer_b24(cfg)
    profile = cfg.profiles["aether-gazer"]
    if where == "elsewhere":
        outside = tmp_path / "Games" / "AG"
        (outside / "prefix" / "drive_c").mkdir(parents=True)
        profile["prefix"] = str(outside / "prefix")
        profile["install_dir"] = str(outside / "install")
    elif where == "other_profiles_folder":
        profile["prefix"] = str(paths.profiles_dir() / "someone-else" / "prefix")
    elif where == "install_outside":
        profile["install_dir"] = str(tmp_path / "Games" / "AG")
    else:
        profile["owned_trees"] = ["install_dir"]  # the player's own prefix: keep it so
    libraries.backfill(cfg)
    owned = cfg.profiles["aether-gazer"].get("owned_trees")
    if where == "already_recorded":
        assert owned == ["install_dir"]
    else:
        assert owned is None
    assert home.is_dir()


# ---- 4: a game that was never installed ---------------------------------------------------
def test_startup_does_not_write_a_manifest_into_a_folder_without_the_game(env):
    """A backend's own state folder is not a game folder."""
    cfg, _home = env
    folder = paths.profiles_dir() / "bd2"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text("{}")
    cfg.add_profile(
        "bd2",
        backend=KIND,
        title="Brown Dust 2",
        prefix=str(folder / "prefix"),
        install_dir=str(folder / "install"),
    )
    libraries.backfill(cfg)
    assert not (folder / libraries.MANIFEST_NAME).exists()


def test_a_manifest_only_folder_is_reused_not_doubled(env):
    """The Brown Dust 2 leftover must not push a reinstall to 'Brown Dust 2 2'."""
    cfg, home = env
    nadare = home / "Games" / libraries.NADARE_DIR
    stale = nadare / "Brown Dust 2"
    stale.mkdir(parents=True)
    (stale / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": KIND,
                "title": "Brown Dust 2",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    (stale / TMP_NAME).write_text("{")
    assert relink.free_folder(nadare, "Brown Dust 2", {}) == stale
    assert (stale / libraries.MANIFEST_NAME).is_file()  # asking changes nothing (review A9)
    assert relink.clear_stale_folder(stale, {}) and not stale.exists()


def test_a_folder_with_game_files_is_still_not_free(env):
    cfg, home = env
    nadare = home / "Games" / libraries.NADARE_DIR
    game = nadare / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / libraries.MANIFEST_NAME).write_text("{}")
    assert relink.free_folder(nadare, "Brown Dust 2", {}) == nadare / "Brown Dust 2 2"
    assert (game / libraries.MANIFEST_NAME).is_file()


def test_a_folder_claimed_by_a_profile_is_never_cleared(env):
    cfg, home = env
    nadare = home / "Games" / libraries.NADARE_DIR
    stale = nadare / "Brown Dust 2"
    stale.mkdir(parents=True)
    (stale / libraries.MANIFEST_NAME).write_text("{}")
    profiles = {"bd2": {"backend": KIND, "owned_dirs": [str(stale)]}}
    assert relink.free_folder(nadare, "Brown Dust 2", profiles) == nadare / "Brown Dust 2 2"
    assert (stale / libraries.MANIFEST_NAME).is_file()


def test_change_location_ignores_a_lone_manifest(env):
    from nadare.gui.settings_parts import prefix_tools

    folder = env[1] / "x"
    folder.mkdir()
    (folder / libraries.MANIFEST_NAME).write_text("{}")
    (folder / TMP_NAME).write_text("{")
    assert prefix_tools.has_game_files(folder) is False
    (folder / "prefix").mkdir()
    assert prefix_tools.has_game_files(folder) is True
