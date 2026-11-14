# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F3: the manifest keeps an old title because sync ignores title changes."""
from __future__ import annotations

from nadare.core import libraries
from nadare.core.config import Config


def test_sync_rewrites_a_stale_manifest_title(tmp_path):
    library = tmp_path / "Games"
    game_dir = library / libraries.NADARE_DIR / "Blue Archive"
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "blue", backend="direct-bd2", title="Blue Archive",
        install_dir=str(game_dir / "install"), prefix=str(game_dir / "prefix"),
        owned_dirs=[str(game_dir)], owned_trees=["install_dir", "prefix"],
    )
    profile = config.profiles["blue"]
    assert libraries.write_manifest(profile, game_dir) is not None
    assert libraries.read_manifest(game_dir).title == "Blue Archive"

    profile["title"] = "Blue Archive (JP)"
    libraries.sync_game(config, "blue")
    manifest = libraries.read_manifest(game_dir)
    print("profile title:", profile["title"], "manifest title:", manifest.title)
    assert manifest.title == profile["title"]
