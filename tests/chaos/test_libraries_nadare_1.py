# SPDX-License-Identifier: GPL-3.0-or-later
"""A game whose folder is literally named ``Nadare`` is still a game.

The scan skips an entry named ``Nadare`` because that can be the nested b21 library
folder. A real game folder of that name (a game titled ``Nadare``) has a manifest and
must be found, otherwise losing the config loses the game.
"""

from __future__ import annotations

import json

import pytest

from nadare.core import libraries
from nadare.core.config import Config


@pytest.mark.parametrize("where", ["library", "default"])
def test_a_game_folder_named_nadare_is_found(tmp_path, where):
    if where == "library":
        root = tmp_path / "Games"
        game_dir = root / libraries.NADARE_DIR / libraries.NADARE_DIR
    else:
        root = libraries.default_root()
        game_dir = root / libraries.NADARE_DIR
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "game.exe").write_text("x")
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Nadare",
                "install": "install",
                "made_by_nadare": ["install"],
            }
        )
    )

    cfg = Config.load(tmp_path / "config.json")
    found, notes = libraries.scan(cfg, root)

    print("root:", root)
    print("game folder:", game_dir)
    print("found:", [c.game_dir for c in found])
    print("notes:", notes)

    assert [c.game_dir for c in found] == [game_dir]
    assert notes == []
