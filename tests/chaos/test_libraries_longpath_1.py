# SPDX-License-Identifier: GPL-3.0-or-later
"""A manifest path longer than NAME_MAX is damaged input, not a reason to crash the scan."""

from __future__ import annotations

import json

from nadare.core import libraries
from nadare.core.config import Config


def test_a_too_long_manifest_folder_is_skipped_with_a_note(tmp_path):
    lib = tmp_path / "Games"
    game = lib / "Nadare" / "Game"
    (game / "install").mkdir(parents=True)
    (game / "install" / "game.exe").write_text("x")

    too_long = "a" * 300  # one component, over the usual NAME_MAX of 255
    (game / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Game",
                "install": too_long,
                "made_by_nadare": ["install"],
            }
        )
    )

    cfg = Config.load(tmp_path / "config.json")
    try:
        found, notes = libraries.scan(cfg, lib)
    except OSError as exc:
        print("manifest install component length:", len(too_long))
        print("scan raised:", type(exc).__name__, exc)
        raise AssertionError(
            "a damaged manifest path made libraries.scan() raise OSError"
        ) from exc

    print("found:", found)
    print("notes:", notes)
    assert found == []
    assert notes, "the damaged manifest should be skipped with a note"
