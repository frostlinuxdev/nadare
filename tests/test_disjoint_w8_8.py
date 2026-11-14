# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-8: Find games must see CZN's version from its UPF, not only the direct version file."""

from __future__ import annotations

import json

from nadare.core import gamefinder, libraries
from nadare.core.config import Config


def test_found_czn_copy_has_the_upf_version(tmp_path):
    lib = tmp_path / "Games"
    game_dir = lib / libraries.NADARE_DIR / "Chaos Zero Nightmare"
    prefix = game_dir / "prefix"
    game = prefix / "drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare"
    (game / "bin").mkdir(parents=True)
    (game / "bin" / "ssr-stove-shield.exe").write_bytes(b"shield")
    upf = game / "combinedata_manifest" / "GameManifest_STOVE_CHAOSZERO.upf"
    upf.parent.mkdir(parents=True)
    upf.write_text(json.dumps({"local_version": 42}))

    cfg = Config.load(tmp_path / "config.json")
    profile = cfg.add_profile(
        "czn",
        backend="stove-czn",
        title="Chaos Zero Nightmare",
        prefix=str(prefix),
        owned_dirs=[str(game_dir)],
        owned_trees=["prefix"],
    )
    libraries.write_manifest(profile, game_dir)
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(lib)}]

    copies = gamefinder.find_copies(cfg, "czn", None)
    print("copies:", [(str(c.game_dir), c.version) for c in copies])
    assert copies, "fixture: CZN copy should be found"
    print("found version:", copies[0].version, "| page/UPF version: 42")
    assert copies[0].version in ("42", "v42"), (
        "Find games only reads .nadare-version.json, so the CZN copy looks versionless"
    )
