# SPDX-License-Identifier: GPL-3.0-or-later
"""A b22/b23 game in the default library whose profile name differs from its folder.

Old Add Game named the folder from the game title but let the player pick any profile name.
For the default library the folder is ``profiles/<title-slug>``, not ``profiles/<profile>``.
b22 wrote no ``nadare-game.json`` (libraries did not exist); b23's own backfill also looked
only at ``profiles/<profile>``, so such a game still has none.  b24 claims to bring back games
kept in its own folder, but it repeats the same guess, never writes the manifest where the game
really is, and Find games cannot see it after the config is lost.
"""

from __future__ import annotations

from nadare.core import libraries
from nadare.core.config import Config


def _old_default_game(default):
    folder = default / "brown-dust-2"  # Add Game's folder name from the title
    (folder / "install").mkdir(parents=True)
    (folder / "install" / "game.exe").write_text("x")
    (folder / "prefix" / "drive_c").mkdir(parents=True)
    return folder.resolve()


def test_a_default_library_game_with_a_custom_profile_name_is_found(tmp_path):
    default = libraries.default_root()
    game = _old_default_game(default)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "my-brown-dust-2",  # the player chose this profile name; the folder is title-named
        backend="direct-bd2",
        title="Brown Dust 2",
        install_dir=str(game / "install"),
        prefix=str(game / "prefix"),
        owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str(game / "install"), "prefix": str(game / "prefix")},
    )

    libraries.backfill(cfg)
    actual_manifest = game / libraries.MANIFEST_NAME
    guessed_manifest = default / "my-brown-dust-2" / libraries.MANIFEST_NAME
    print("manifest in the real game folder:", actual_manifest.exists())
    print("manifest in the guessed profile folder:", guessed_manifest.exists())

    cfg.remove_profile("my-brown-dust-2")
    found, notes = libraries.scan(cfg, default)
    print("default-library scan:", [str(c.game_dir) for c in found], notes)
    assert [str(c.game_dir) for c in found] == [str(game)]
