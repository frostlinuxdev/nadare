# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The catalog backend kinds, as plain data.

``direct-<game>`` kinds install straight from the publisher's API;
``launcher-<game>`` kinds run the publisher's own launcher under Wine. Kept free
of imports so the backend registry, the Add Game wizard, the runner defaults and
the library labels can use it without loading any backend.
``tests/test_direct_backend.py`` checks it against :mod:`nadare.backends`.
"""

from __future__ import annotations

import os

#: kind -> (class name in ``nadare.backends.direct``, title, install root, sub-folder, exe)
_DIRECT: dict[str, tuple[str, str, str, str, str]] = {
    "direct-bd2": (
        "BrownDust2Backend",
        "Brown Dust 2",
        "drive_c/Games",
        "BrownDust2",
        "BrownDust II.exe",
    ),
    "direct-aethergazer": (
        "AetherGazerBackend",
        "Aether Gazer",
        "drive_c/Games",
        "AetherGazer",
        "AetherGazer.exe",
    ),
    "direct-mongil": (
        "MongilBackend",
        "Mongil: Star Dive",
        "drive_c/Games",
        "MongilStarDive",
        "STARDIVE.exe",
    ),
    "direct-wuwa": (
        "WutheringWavesBackend",
        "Wuthering Waves",
        "drive_c/Games",
        "WutheringWaves",
        "Wuthering Waves.exe",
    ),
    "direct-gfl2": (
        "Gfl2Backend",
        "Girls' Frontline 2: Exilium (Darkwinter build)",
        "drive_c/Games",
        "GF2Exilium",
        "GF2_Exilium.exe",
    ),
    "direct-endfield": (
        "EndfieldBackend",
        "Arknights: Endfield",
        "drive_c/Games",
        "ArknightsEndfield",
        "Endfield.exe",
    ),
    "direct-pgr": (
        "PgrBackend",
        "Punishing: Gray Raven",
        "drive_c/Games",
        "PunishingGrayRaven",
        "PGR.exe",
    ),
    "direct-nte": (
        "NteDirectBackend",
        "Neverness to Everness",
        "drive_c/Games",
        "NevernessToEverness",
        "Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe",
    ),
    "direct-gfl2-haoplay": (
        "Gfl2HaoplayBackend",
        "Girls' Frontline 2 (Haoplay global build)",
        "drive_c/Games",
        "GF2Haoplay",
        "GF2_Exilium.exe",
    ),
    "direct-genshin": (
        "GenshinBackend",
        "Genshin Impact",
        "drive_c/Games",
        "GenshinImpact",
        "GenshinImpact.exe",
    ),
    "direct-hsr": (
        "StarRailBackend",
        "Honkai: Star Rail",
        "drive_c/Games",
        "HonkaiStarRail",
        "StarRail.exe",
    ),
    "direct-hi3": (
        "Hi3Backend",
        "Honkai Impact 3rd",
        "drive_c/Games",
        "HonkaiImpact3rd",
        "BH3.exe",
    ),
    "direct-zzz": (
        "ZenlessBackend",
        "Zenless Zone Zero",
        "drive_c/Games",
        "ZenlessZoneZero",
        "ZenlessZoneZero.exe",
    ),
}

#: kind -> (class name in ``nadare.backends.launcher``, title, install root, sub-folder,
#: launcher exe)
_LAUNCHER: dict[str, tuple[str, str, str, str, str]] = {
    "launcher-nikke": (
        "NikkeBackend",
        "NIKKE: Goddess of Victory",
        "drive_c",
        "NIKKE",
        "nikke_launcher.exe",
    ),
    "launcher-nte": (
        "NteBackend",
        "Neverness to Everness (publisher's launcher)",
        "drive_c/Program Files",
        "Neverness To Everness",
        "NTEGlobalLauncher.exe",
    ),
    "launcher-gfl2-haoplay": (
        "Gfl2HaoplayBackend",
        "Girls' Frontline 2 (Haoplay global build, publisher's launcher)",
        "drive_c/Program Files",
        "GF2",
        "PCLauncher.exe",
    ),
}

#: Steam app id (or ``None``) whose store art a catalog game uses; the publisher's own page
#: supplies the cover when there is none (see ``artwork._PUBLISHER_OGP``). Every catalog kind
#: has a key.
ART: dict[str, int | None] = {
    "direct-bd2": None,
    "direct-aethergazer": None,
    "direct-mongil": None,
    "direct-wuwa": 3513350,
    "direct-pgr": 4125930,
    "direct-gfl2": 3347400,
    "direct-endfield": 4732690,
    "direct-nte": 4508340,
    "direct-gfl2-haoplay": 3308670,
    "direct-genshin": None,
    "direct-hsr": None,
    "direct-hi3": None,
    "direct-zzz": 4162040,
    "launcher-nikke": None,
    "launcher-nte": 4508340,
    "launcher-gfl2-haoplay": 3308670,
}

DIRECT_KINDS = frozenset(_DIRECT)
LAUNCHER_KINDS = frozenset(_LAUNCHER)
#: Every kind the catalog adds (all run through a Wine/Proton prefix).
CATALOG_KINDS = DIRECT_KINDS | LAUNCHER_KINDS

#: kind -> "module:Class" for the registry.
CATALOG_BACKENDS: dict[str, str] = {
    **{kind: f"nadare.backends.direct:{row[0]}" for kind, row in _DIRECT.items()},
    **{kind: f"nadare.backends.launcher:{row[0]}" for kind, row in _LAUNCHER.items()},
}

#: Wizard rows: (kind, title, install root, sub-folder, exe), direct games first.
DIRECT_WIZARD: tuple[tuple[str, str, str, str, str], ...] = tuple(
    (kind, *row[1:]) for kind, row in _DIRECT.items()
)
LAUNCHER_WIZARD: tuple[tuple[str, str, str, str, str], ...] = tuple(
    (kind, *row[1:]) for kind, row in _LAUNCHER.items()
)

#: Support tiers shown as a badge in the Add Game wizard. A kind is Verified once a
#: maintainer has installed it through Nadare and played it past the title screen on a
#: real machine; every other catalog kind is Experimental. (A Community tier for
#: reviewed third-party entries is planned but has no entries yet.)
TIER_VERIFIED = "Verified"
TIER_EXPERIMENTAL = "Experimental"
VERIFIED_KINDS = frozenset(
    {
        "direct-bd2",
        "direct-aethergazer",
        "direct-pgr",
        "direct-gfl2",
        "direct-hi3",
        "direct-endfield",
        "direct-nte",
    }
)

#: HoYo's games. They are offered like the rest; ``NADARE_HIDE_HOYO=1`` keeps them out of the
#: Add Game wizard and the published catalog (a profile that already exists still works).
HOYO_KINDS = frozenset({"direct-genshin", "direct-hsr", "direct-hi3", "direct-zzz"})


def hoyo_visible() -> bool:
    """True when the HoYo games should be offered for new profiles."""
    return os.environ.get("NADARE_HIDE_HOYO") != "1"


def tier_of(kind: str) -> str:
    """The support tier of a catalog ``kind``."""
    return TIER_VERIFIED if kind in VERIFIED_KINDS else TIER_EXPERIMENTAL


#: Tooltip per tier.
TIER_HELP = {
    TIER_VERIFIED: "Installed through Nadare and played on a real machine by a maintainer.",
    TIER_EXPERIMENTAL: (
        "Community-tested at best; it may break when the publisher changes something."
    ),
}

#: Friendly labels for cards and pages.
DIRECT_LABEL = "Direct download"
LAUNCHER_LABEL = "Publisher's launcher"
