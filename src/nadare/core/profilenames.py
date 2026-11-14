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

from collections.abc import Mapping
from typing import Any

#: Wizard titles that do not identify one game. A shortcut label made from one of these
#: must carry the profile key, or every advanced game looks the same in the menu.
GENERIC_TITLES = frozenset({"Wine / Proton game", "Steam game", "Script"})


def profile_title(name: str, profile: Mapping[str, Any]) -> str:
    """The display title for a profile (falls back to the profile key)."""
    return str(profile.get("title") or name)


def profile_display(
    name: str,
    profile: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """A game label, with ``" · <profile>"`` when the title alone is ambiguous.

    ``profiles`` is the whole library when the caller has it. Without it, a wizard's fixed
    generic title still gets the profile key so two advanced games never share one label.
    """
    title = profile_title(name, profile)
    if profiles is None:
        return f"{title} · {name}" if title in GENERIC_TITLES else title
    folded = title.casefold()
    if any(
        other != name
        and isinstance(data, Mapping)
        and profile_title(other, data).casefold() == folded
        for other, data in profiles.items()
    ):
        return f"{title} · {name}"
    return title


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


__all__ = ["GENERIC_TITLES", "SHORT_NAMES", "profile_display", "profile_title", "short_name"]
