# SPDX-License-Identifier: GPL-3.0-or-later
"""Short default profile names, one per game kind (#1134).

A profile name is the folder under ``profiles/`` (prefix, install, logs), the config key and the
``--profile`` argument, so it should be the short name players use, not a slug of the catalog
title (``girls-frontline-2-haoplay-global-build-publisher-s-launcher``). The names are written
down, never abbreviated automatically. The display title is unchanged and stays the full title.

Only new profiles get these names; an existing profile keeps its name. A kind not listed here
(the generic Wine, Steam and script kinds) keeps the slug of its title.
"""

from __future__ import annotations

SHORT_NAMES: dict[str, str] = {
    "arknights-yostar": "arknights",
    "stellasora-yostar": "stella-sora",
    "stove-czn": "czn",
    "fgo-lepton": "fgo",
    "direct-bd2": "bd2",
    "direct-aethergazer": "aether-gazer",
    "direct-mongil": "mongil",
    "direct-wuwa": "wuwa",
    "direct-endfield": "endfield",
    "direct-pgr": "pgr",
    "direct-nte": "nte",
    "direct-genshin": "genshin",
    "direct-hsr": "hsr",
    "direct-hi3": "hi3",
    "direct-zzz": "zzz",
    "direct-gfl2": "gfl2",
    "direct-gfl2-haoplay": "gfl2-hp",
    "launcher-nikke": "nikke",
}


def short_name(kind: str) -> str:
    """The short default profile name for ``kind``, or ``""`` when it has none."""
    return SHORT_NAMES.get(kind, "")


__all__ = ["SHORT_NAMES", "short_name"]
