# SPDX-License-Identifier: GPL-3.0-or-later
"""A manifest path over NAME_MAX must not crash the search for usable game copies."""

from __future__ import annotations

import json

from nadare.core import gamefinder, libraries
from nadare.core.config import Config


def test_find_copies_does_not_crash_on_a_too_long_manifest_path(tmp_path):
    lib = tmp_path / "Games"
    game = lib / "Nadare" / "Game"
    (game / "prefix" / "drive_c").mkdir(parents=True)
    too_long = "a" * 300
    (game / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Game",
                "install": too_long,
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )

    cfg = Config.load(tmp_path / "config.json")
    libraries.add_library(cfg, lib)
    cfg.add_profile(
        "game",
        backend="direct-bd2",
        title="Game",
        install_dir=str(game / "missing"),
        prefix=str(game / "prefix"),
        owned_dirs=[str(game)],
    )

    try:
        copies = gamefinder.find_copies(cfg, "game", None)
    except OSError as exc:
        print("manifest install component length:", len(too_long))
        print("find_copies raised:", type(exc).__name__, exc)
        raise AssertionError(
            "a damaged manifest path made find_copies() raise OSError"
        ) from exc

    print("copies:", copies)
    assert all(not copy.complete for copy in copies), copies
