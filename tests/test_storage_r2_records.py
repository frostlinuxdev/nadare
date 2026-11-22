# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage review round 2 (opencode-go Flash, 2026-11-14): records, guards and damaged configs.

A1: one profile with a NUL in a path broke Remove for every game.
A2/B5: a NUL library path or profile path stopped the start-up upkeep for every game.
B1: a hand-added profile "Game/" made the config migration move the real "Game"'s folder.
B4: Add Game could place a game inside another game's own Nadare/<Game> folder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import libraries, paths, prefixguard, removal
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json"), home


def _game(cfg: Config, name: str) -> Path:
    home = paths.profiles_dir() / name
    (home / "prefix" / "drive_c").mkdir(parents=True)
    (home / "install").mkdir()
    cfg.add_profile(
        name,
        backend="direct-bd2",
        title=name,
        prefix=str(home / "prefix"),
        install_dir=str(home / "install"),
        owned_trees=["install_dir", "prefix"],
    )
    return home


def test_a_nul_in_another_profile_does_not_block_remove(env, tmp_path):
    cfg, _home = env
    _game(cfg, "bd2")
    cfg.add_profile("other", backend="wine", title="Other", prefix="/mnt/x\x00y")
    trees = removal.removable_trees(cfg, "bd2")
    assert {t.key for t in trees if t.safe} == {"install_dir", "prefix"}


def test_a_nul_library_path_does_not_stop_the_upkeep(env):
    cfg, _home = env
    home = _game(cfg, "bd2")
    cfg.data[libraries.CONFIG_KEY] = [{"path": "/mnt/x\x00y/Nadare"}]
    libraries.backfill(cfg)
    assert (home / libraries.MANIFEST_NAME).is_file()


def test_a_nul_profile_does_not_stop_the_upkeep_for_the_others(env):
    cfg, _home = env
    broken = _game(cfg, "aaa-broken")  # a real game folder, a NUL only in the prefix
    cfg.profiles["aaa-broken"]["prefix"] = str(broken / "prefix") + "\x00evil"
    cfg.profiles["aaa-broken"].pop("owned_trees")  # the b22..b24 shape the migration reads
    home = _game(cfg, "zzz-fine")
    libraries.backfill(cfg)
    assert (home / libraries.MANIFEST_NAME).is_file()


def test_a_trailing_slash_profile_never_moves_the_real_games_folder(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    root = paths.profiles_dir()
    (root / "Game" / "install").mkdir(parents=True)
    (root / "Game" / "install" / "game.exe").write_text("x")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "Game": {
                        "backend": "direct-bd2",
                        "title": "Game",
                        "install_dir": str(root / "Game" / "install"),
                    },
                    "Game/": {"backend": "wine", "title": "Copy"},
                }
            }
        )
    )
    cfg = Config.load(cfg_path)
    assert (root / "Game" / "install" / "game.exe").is_file()
    assert cfg.profiles["Game"]["install_dir"] == str(root / "Game" / "install")


def test_another_games_own_library_folder_is_already_used(env):
    cfg, home = env
    alpha = home / "Games" / "Nadare" / "Alpha"
    (alpha / "install").mkdir(parents=True)
    cfg.add_profile(
        "alpha",
        backend="direct-bd2",
        title="Alpha",
        install_dir=str(alpha / "install"),
        owned_dirs=[str(alpha)],
    )
    inside = alpha / "Nadare" / "Beta" / "install"
    assert prefixguard.unsafe_reason(inside, profiles=cfg.profiles, own="beta")
