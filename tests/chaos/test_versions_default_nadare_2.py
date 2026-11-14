# SPDX-License-Identifier: GPL-3.0-or-later
"""A game b23 moved into the default library under ``profiles/Nadare`` must be findable.

b23's Move into a library allowed Nadare's own profiles folder as the library and put the
game in ``profiles/Nadare/<Game>`` (the gap b24 closes for future Moves).  b24 already calls
that folder part of the default library when it shows placement, but its Find games scan never
looks in the ``Nadare`` folder of the default library, so a lost config makes the game
invisible.
"""

from __future__ import annotations

import json

from nadare.core import gamefolders, libraries
from nadare.core.config import Config


def _game(folder):
    (folder / "install").mkdir(parents=True)
    (folder / "install" / "game.exe").write_text("x")
    (folder / "prefix" / "drive_c").mkdir(parents=True)
    (folder / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "My Game",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    return folder.resolve()


def test_find_games_reads_the_default_librarys_nadare_folder(tmp_path):
    default = libraries.default_root()
    game = _game(default / libraries.NADARE_DIR / "My Game")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "bd2",
        backend="direct-bd2",
        title="My Game",
        install_dir=str(game / "install"),
        prefix=str(game / "prefix"),
        owned_dirs=[str(game)],
        owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str(game / "install"), "prefix": str(game / "prefix")},
    )
    place = gamefolders.placement(cfg, "bd2")
    print("b24 placement says:", place)
    assert place is not None and place.library == default

    # The config is lost (or the profile was removed with its files kept); Find games is the
    # only way back.
    cfg.remove_profile("bd2")
    found, notes = libraries.scan(cfg, default)
    print("default-library scan:", [str(c.game_dir) for c in found], notes)
    assert [str(c.game_dir) for c in found] == [str(game)]
