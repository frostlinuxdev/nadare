# SPDX-License-Identifier: GPL-3.0-or-later
"""Every shape a game's folders and records take in the wild (#1372 phase 0).

Each shape builds its folders under a temp base and returns the config data that goes with
them. ``tests/test_storage_shapes.py`` records what Nadare answers today for each game in each
shape ("whose folder is it", "may Uninstall delete it", "is it used by another game", ...)
into a golden file, so the storage consolidation can change those answers only on purpose.

Placeholders in paths: ``{HOME}`` the home, ``{DATA}`` Nadare's data folder, ``{LIB}`` and
``{LIB2}`` library drives, ``{MNT}`` the maintainer's library drive (flxpc world).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The 22 shapes; most come from the inventory in #1372 (section "old-config shapes") and the
#: storage review rounds of 2026-11-14.
Builder = Callable[["World"], dict[str, Any]]


@dataclass
class World:
    base: Path

    def path(self, text: str) -> Path:
        return Path(self.fill(text))

    def fill(self, text: str) -> str:
        return (
            text.replace("{HOME}", str(self.base / "home"))
            .replace("{DATA}", str(self.base / "data" / "nadare"))
            .replace("{LIB2}", str(self.base / "lib2"))
            .replace("{LIB3}", str(self.base / "lib3"))
            .replace("{LIB}", str(self.base / "lib"))
            .replace("{MNT}", str(self.base / "mnt"))
        )

    def prefix(self, text: str) -> str:
        folder = self.path(text)
        (folder / "drive_c" / "windows").mkdir(parents=True, exist_ok=True)
        (folder / "system.reg").write_text("reg")
        return str(folder)

    def install(self, text: str, exe: str = "game.exe") -> str:
        folder = self.path(text)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / exe).write_bytes(b"MZ")
        return str(folder)

    def manifest(self, folder: str, **data: Any) -> None:
        body = {"schema": 1, **data}
        self.path(folder).mkdir(parents=True, exist_ok=True)
        (self.path(folder) / "nadare-game.json").write_text(json.dumps(body))


def _owned(world: World, **trees: str) -> dict[str, Any]:
    return {
        "owned_trees": sorted(trees),
        "owned_paths": {k: str(Path(world.fill(v)).resolve()) for k, v in trees.items()},
    }


def s01_add_game_library(w: World) -> dict[str, Any]:
    """Today's Add Game: <lib>/Nadare/<Game>/{install,prefix}, every record, a manifest."""
    home = "{LIB}/Nadare/Brown Dust 2"
    p = {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "install_dir": w.install(home + "/install", "BrownDust II.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "owned_dirs": [w.fill(home)],
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    w.manifest(
        home,
        backend="direct-bd2",
        title="Brown Dust 2",
        install="install",
        prefix="prefix",
        made_by_nadare=["install", "prefix"],
    )
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"bd2": p}}


def s02_default_library_no_records(w: World) -> dict[str, Any]:
    """b22-b24 default library, no ownership records (flxpc aether-gazer)."""
    home = "{DATA}/profiles/aether-gazer"
    p = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": w.install(home + "/install", "AetherGazer.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "installed_prefix": w.fill(home + "/prefix"),
    }
    w.manifest(
        home,
        backend="direct-aethergazer",
        title="Aether Gazer",
        install="install",
        prefix="prefix",
        made_by_nadare=[],
    )
    return {"profiles": {"aether-gazer": p}}


def s03_default_library_trees_no_dirs(w: World) -> dict[str, Any]:
    """Default library with owned_trees + owned_paths but no owned_dirs (flxpc wuwa)."""
    home = "{DATA}/profiles/wuthering-waves"
    p = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": w.install(home + "/install", "Wuthering Waves.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "installed_prefix": w.fill(home + "/prefix"),
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    return {"profiles": {"wuthering-waves": p}}


def s04_b13_no_paths(w: World) -> dict[str, Any]:
    """b13 and older: owned_trees but no owned_paths, library layout, no owned_dirs."""
    home = "{LIB}/Nadare/Genshin Impact"
    p = {
        "backend": "direct-genshin",
        "title": "Genshin Impact",
        "install_dir": w.install(home + "/install", "GenshinImpact.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "owned_trees": ["install_dir", "prefix"],
    }
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"genshin": p}}


def s05_legacy_layout_with_paths(w: World) -> dict[str, Any]:
    """Before owned_dirs (b17-): the library layout with owned_trees + owned_paths."""
    home = "{LIB}/Nadare/Honkai Star Rail"
    p = {
        "backend": "direct-hsr",
        "title": "Honkai: Star Rail",
        "install_dir": w.install(home + "/install", "StarRail.exe"),
        "prefix": w.prefix(home + "/prefix"),
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"hsr": p}}


def s06_b21_nested_container(w: World) -> dict[str, Any]:
    """b21: the library stored as <lib>/Nadare and games in <lib>/Nadare/Nadare/<Game>."""
    home = "{LIB}/Nadare/Nadare/Zenless Zone Zero"
    p = {
        "backend": "direct-zzz",
        "title": "Zenless Zone Zero",
        "install_dir": w.install(home + "/install", "ZenlessZoneZero.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "owned_dirs": [w.fill(home)],
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    return {"libraries": [{"path": w.fill("{LIB}/Nadare")}], "profiles": {"zzz": p}}


def s07_moved_into_profiles_nadare(w: World) -> dict[str, Any]:
    """b23: an old Move put the game in profiles/Nadare/<Game>."""
    home = "{DATA}/profiles/Nadare/Punishing Gray Raven"
    p = {
        "backend": "direct-pgr",
        "title": "Punishing: Gray Raven",
        "install_dir": w.install(home + "/install", "PGR.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "owned_dirs": [w.fill(home)],
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    w.manifest(
        home,
        backend="direct-pgr",
        title="Punishing: Gray Raven",
        install="install",
        prefix="prefix",
        made_by_nadare=["install", "prefix"],
    )
    return {"profiles": {"pgr": p}}


def s08_imported_elsewhere(w: World) -> dict[str, Any]:
    """An existing install the player pointed Nadare at; nothing is Nadare's."""
    p = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": w.install("{HOME}/Games/WuWa/Wuthering Waves Game", "Wuthering Waves.exe"),
        "prefix": w.prefix("{HOME}/Games/WuWa/prefix"),
    }
    return {"profiles": {"wuwa-import": p}}


def s09_player_prefix_nadare_install(w: World) -> dict[str, Any]:
    """The player's own prefix, Nadare's install folder."""
    home = "{DATA}/profiles/endfield"
    p = {
        "backend": "direct-endfield",
        "title": "Arknights: Endfield",
        "install_dir": w.install(home + "/install", "Endfield.exe"),
        "prefix": w.prefix("{HOME}/prefixes/endfield"),
        **_owned(w, install_dir=home + "/install"),
    }
    return {"profiles": {"endfield": p}}


def s10_picked_library_folder_no_trees(w: World) -> dict[str, Any]:
    """A folder in a library the player picked, already holding the game: owned_dirs only."""
    home = "{LIB}/Nadare/Mongil"
    p = {
        "backend": "direct-mongil",
        "title": "Mongil: Star Dive",
        "install_dir": w.install(home + "/install", "STARDIVE.exe"),
        "prefix": w.prefix(home + "/prefix"),
        "owned_dirs": [w.fill(home)],
        "owned_trees": [],
    }
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"mongil": p}}


def s11_launcher_not_installed_yet(w: World) -> dict[str, Any]:
    """A launcher game before its launcher installed the game: prefix only."""
    home = "{DATA}/profiles/nikke"
    p = {
        "backend": "launcher-nikke",
        "title": "NIKKE",
        "install_dir": "",
        "prefix": w.prefix(home + "/prefix"),
        **_owned(w, prefix=home + "/prefix"),
    }
    return {"profiles": {"nikke": p}}


def s12_prefix_only_kind(w: World) -> dict[str, Any]:
    """A kind whose game lives inside its prefix (CZN), in a library."""
    home = "{LIB}/Nadare/Chaos Zero Nightmare"
    prefix = w.prefix(home + "/prefix")
    p = {
        "backend": "stove-czn",
        "title": "Chaos Zero Nightmare",
        "prefix": prefix,
        "install_dir": w.install(home + "/prefix/drive_c/CZN", "CZN.exe"),
        "owned_dirs": [w.fill(home)],
        **_owned(w, prefix=home + "/prefix"),
    }
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"czn": p}}


def s13_generic_wine_default_prefix(w: World) -> dict[str, Any]:
    """A generic Wine game with no prefix key: the backend's default profiles/<name>/prefix."""
    w.prefix("{DATA}/profiles/my-tool/prefix")
    p = {"backend": "wine", "title": "My Tool", "exe": w.fill("{HOME}/tools/tool.exe")}
    w.install("{HOME}/tools", "tool.exe")
    return {"profiles": {"my-tool": p}}


def s14_steam_game(w: World) -> dict[str, Any]:
    """A Steam game (flxpc Blue Archive): Steam keeps its own prefix and game files, so Nadare
    records no folder at all, only the Steam app id."""
    p = {
        "backend": "steam",
        "title": "Blue Archive",
        "steam_app_id": "3557620",
        "prefix": "",
        "install_dir": "",
    }
    return {"profiles": {"Blue Archive": p}}


def s15_prefix_moved_out(w: World) -> dict[str, Any]:
    """A prefix-only Move took the prefix out of the game folder (#1366 A3)."""
    home = "{LIB}/Nadare/Aether Gazer"
    p = {
        "backend": "direct-aethergazer",
        "title": "Aether Gazer",
        "install_dir": w.install(home + "/install", "AetherGazer.exe"),
        "prefix": w.prefix("{LIB2}/Aether Gazer prefix"),
        "owned_dirs": [w.fill(home)],
        **_owned(w, install_dir=home + "/install", prefix="{LIB2}/Aether Gazer prefix"),
    }
    return {"libraries": [{"path": w.fill("{LIB}")}], "profiles": {"aether": p}}


def s16_linked_profile_folder(w: World) -> dict[str, Any]:
    """profiles/<name> is a link to a personal folder (storage review B1)."""
    documents = w.path("{HOME}/Documents")
    w.prefix("{HOME}/Documents/prefix")
    profiles = w.path("{DATA}/profiles")
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "gone").symlink_to(documents)
    return {"profiles": {"gone": {"backend": "wine", "title": "Gone"}}}


def s17_library_drive_missing(w: World) -> dict[str, Any]:
    """A library on a drive that is not mounted: every folder is missing."""
    home = "{LIB2}/Nadare/Wuthering Waves"
    p = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": w.fill(home + "/install"),
        "prefix": w.fill(home + "/prefix"),
        "owned_dirs": [w.fill(home)],
        "owned_trees": ["install_dir", "prefix"],
        "owned_paths": {
            "install_dir": w.fill(home + "/install"),
            "prefix": w.fill(home + "/prefix"),
        },
    }
    return {"libraries": [{"path": w.fill("{LIB2}")}], "profiles": {"wuwa": p}}


def s18_profile_name_differs_from_folder(w: World) -> dict[str, Any]:
    """Default library, a custom profile name and a title-named folder (chaos D4)."""
    home = "{DATA}/profiles/Brown Dust 2"
    p = {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "install_dir": w.install(home + "/install", "BrownDust II.exe"),
        "prefix": w.prefix(home + "/prefix"),
        **_owned(w, install_dir=home + "/install", prefix=home + "/prefix"),
    }
    return {"profiles": {"my-bd2": p}}


def s19_prefix_moved_by_hand(w: World) -> dict[str, Any]:
    """The player moved the prefix by hand and used "I moved it myself": Nadare no longer owns
    it, but ``installed_prefix`` still names it (the review's D7: two Uninstall rules)."""
    home = "{DATA}/profiles/gfl2"
    moved = "{HOME}/my-prefixes/gfl2"
    p = {
        "backend": "direct-gfl2",
        "title": "Girls' Frontline 2",
        "install_dir": w.install(home + "/install", "GF2_Exilium.exe"),
        "prefix": w.prefix(moved),
        "installed_prefix": w.fill(moved),
        **_owned(w, install_dir=home + "/install"),
    }
    return {"profiles": {"gfl2": p}}


def s20_linked_catalog_profile(w: World) -> dict[str, Any]:
    """A catalog game whose profiles/<name> is a link to a personal folder (review of #1374)."""
    w.prefix("{HOME}/Documents/prefix")
    profiles = w.path("{DATA}/profiles")
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "linked").symlink_to(w.path("{HOME}/Documents"))
    p = {
        "backend": "direct-bd2",
        "title": "Linked",
        "prefix": w.fill("{DATA}/profiles/linked/prefix"),
    }
    return {"profiles": {"linked": p}}


def s21_mixed_no_records(w: World) -> dict[str, Any]:
    """Nadare's install in profiles/<name>, the player's prefix elsewhere, no records."""
    p = {
        "backend": "direct-bd2",
        "title": "Mixed",
        "install_dir": w.install("{DATA}/profiles/mixed/install", "BrownDust II.exe"),
        "prefix": w.prefix("{HOME}/pfx-mixed"),
    }
    return {"profiles": {"mixed": p}}


def s22_folders_only_under_extra(w: World) -> dict[str, Any]:
    """A game whose folders are set only in ``extra`` (backends read them there), next to
    another game: "used by another game" must still see them."""
    shared = w.install("{HOME}/Games/Shared", "game.exe")
    return {
        "profiles": {
            "gone": {"backend": "wine", "title": "Gone", "install_dir": shared},
            "keep": {"backend": "wine", "title": "Keep", "extra": {"install_dir": shared}},
        }
    }


def flxpc_world(w: World) -> dict[str, Any]:
    """The maintainer's real profiles on flxpc (2026-11-14), location keys only, paths
    templated; every folder they name is created (the real disk has them)."""
    data = json.loads((Path(__file__).parent / "data" / "storage_world_flxpc.json").read_text())
    text = w.fill(json.dumps(data))
    data = json.loads(text)
    for profile in data["profiles"].values():
        for key in ("prefix", "installed_prefix"):
            value = profile.get(key)
            if isinstance(value, str) and value.startswith("/"):
                w.prefix(value)
        value = profile.get("install_dir")
        if isinstance(value, str) and value.startswith("/"):
            w.install(value)
    return data


SHAPES: dict[str, Builder] = {
    fn.__name__: fn
    for fn in (
        s01_add_game_library,
        s02_default_library_no_records,
        s03_default_library_trees_no_dirs,
        s04_b13_no_paths,
        s05_legacy_layout_with_paths,
        s06_b21_nested_container,
        s07_moved_into_profiles_nadare,
        s08_imported_elsewhere,
        s09_player_prefix_nadare_install,
        s10_picked_library_folder_no_trees,
        s11_launcher_not_installed_yet,
        s12_prefix_only_kind,
        s13_generic_wine_default_prefix,
        s14_steam_game,
        s15_prefix_moved_out,
        s16_linked_profile_folder,
        s17_library_drive_missing,
        s18_profile_name_differs_from_folder,
        s19_prefix_moved_by_hand,
        s20_linked_catalog_profile,
        s21_mixed_no_records,
        s22_folders_only_under_extra,
        flxpc_world,
    )
}
