# SPDX-License-Identifier: GPL-3.0-or-later
"""A game folder's manifest follows the config's ownership record (C2, 2026-11-21).

``nadare-game.json`` records which of the game's folders Nadare made (``made_by_nadare``); the
config records the same in ``owned_trees``. ``sync_game`` rewrote the manifest only when its
folders, kind or title differed, so the first answer stuck: the b22-b24 backfill, a Move, an
import move or a prefix remake changed ``owned_trees`` and never reached the manifest. Readers of
the manifest (Find games adopting a copy, "use this copy") then set ``owned_trees`` back to
``[]``: Move offered the prefix only and Uninstall kept files Nadare made (seen in sim r3b03).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core import libraries, paths
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return Config.load(tmp_path / "config.json")


def _profiles_game(cfg: Config, name: str = "bd2") -> Path:
    """A b22-b24 game in profiles/<name>/{prefix,install}, with no ownership record yet."""
    home = paths.profiles_dir() / name
    (home / "prefix" / "drive_c").mkdir(parents=True)
    (home / "install").mkdir(parents=True)
    (home / "install" / "game.exe").write_text("exe")
    cfg.add_profile(
        name,
        backend="direct-bd2",
        title="Brown Dust 2",
        prefix=str(home / "prefix"),
        install_dir=str(home / "install"),
    )
    cfg.profiles[name].pop("owned_trees", None)
    return home


def _made(home: Path) -> list[str]:
    return json.loads((home / libraries.MANIFEST_NAME).read_text(encoding="utf-8"))[
        "made_by_nadare"
    ]


def test_the_backfill_reaches_a_manifest_written_before_it(env):
    """The r3b03 home: made_by_nadare [] beside owned_trees [install_dir, prefix]."""
    cfg = env
    home = _profiles_game(cfg)
    libraries.sync_game(cfg, "bd2")
    assert _made(home) == []  # written while nothing was recorded
    assert libraries.record_profile_dir_ownership(cfg.profiles["bd2"], "bd2")
    libraries.sync_game(cfg, "bd2")
    assert sorted(_made(home)) == ["install", "prefix"]


def test_backfill_writes_the_record_and_the_manifest_in_one_start(env):
    cfg = env
    home = _profiles_game(cfg)
    libraries.sync_game(cfg, "bd2")  # an older Nadare's start left [] on disk
    libraries.backfill(cfg)
    assert sorted(cfg.profiles["bd2"]["owned_trees"]) == ["install_dir", "prefix"]
    assert sorted(_made(home)) == ["install", "prefix"]


def test_a_narrower_record_narrows_the_manifest(env):
    """A record that leaves a tree out is the player's choice; the manifest must say the same."""
    cfg = env
    home = _profiles_game(cfg)
    cfg.profiles["bd2"]["owned_trees"] = ["install_dir", "prefix"]
    libraries.sync_game(cfg, "bd2")
    cfg.profiles["bd2"]["owned_trees"] = ["prefix"]
    libraries.sync_game(cfg, "bd2")
    assert _made(home) == ["prefix"]


def test_a_manifest_in_agreement_is_not_rewritten(env):
    cfg = env
    home = _profiles_game(cfg)
    cfg.profiles["bd2"]["owned_trees"] = ["install_dir", "prefix"]
    libraries.sync_game(cfg, "bd2")
    file = home / libraries.MANIFEST_NAME
    os.utime(file, (1_000_000, 1_000_000))
    libraries.sync_game(cfg, "bd2")
    assert file.stat().st_mtime == 1_000_000


def test_a_record_naming_a_folder_outside_the_game_folder_is_not_claimed(env, tmp_path):
    """A prefix moved out (prefix-only Move) is not described, so it cannot be made_by_nadare."""
    cfg = env
    home = _profiles_game(cfg)
    outside = tmp_path / "elsewhere" / "prefix"
    (outside / "drive_c").mkdir(parents=True)
    cfg.profiles["bd2"]["prefix"] = str(outside)
    cfg.profiles["bd2"]["owned_trees"] = ["install_dir", "prefix"]
    libraries.sync_game(cfg, "bd2")
    assert _made(home) == ["install"]
    os.utime(home / libraries.MANIFEST_NAME, (1_000_000, 1_000_000))
    libraries.sync_game(cfg, "bd2")  # and that answer counts as in agreement
    assert (home / libraries.MANIFEST_NAME).stat().st_mtime == 1_000_000
