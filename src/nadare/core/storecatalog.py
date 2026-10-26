# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Headless model for Nadare's built-in game store."""

from __future__ import annotations

from dataclasses import dataclass

from . import backends
from .artwork import publisher_art_kinds
from .directkinds import (
    ART,
    DIRECT_LABEL,
    DIRECT_WIZARD,
    HOYO_KINDS,
    LAUNCHER_LABEL,
    LAUNCHER_WIZARD,
    TIER_EXPERIMENTAL,
    TIER_HELP,
    TIER_VERIFIED,
    hoyo_visible,
    tier_of,
)

INSTALLER_LABEL = "Publisher's installer"
CONTAINER_LABEL = "Android container"

#: Older backends the Add Game wizard offers but ``directkinds`` does not list.
_LEGACY_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("stove-czn", "Chaos Zero Nightmare", DIRECT_LABEL, TIER_VERIFIED),
    ("arknights-yostar", "Arknights", INSTALLER_LABEL, TIER_VERIFIED),
    ("stellasora-yostar", "Stella Sora", INSTALLER_LABEL, TIER_VERIFIED),
    ("fgo-lepton", "Fate/Grand Order", CONTAINER_LABEL, TIER_EXPERIMENTAL),
)


@dataclass(frozen=True)
class StoreEntry:
    """Facts displayed for one built-in catalog game."""

    kind: str
    title: str
    tier: str
    tier_help: str
    delivery_label: str
    size_bytes: int | None
    has_art: bool
    notice: tuple[str, str] | tuple[str, str, str] | None
    installed: bool = False


def _catalog_rows() -> list[tuple[str, str, str, str]]:
    """Return kind/title/delivery/tier rows from the same plain source as the wizard."""
    return [
        *(
            (kind, title, DIRECT_LABEL, tier_of(kind))
            for kind, title, *_ in DIRECT_WIZARD
        ),
        *(
            (kind, title, LAUNCHER_LABEL, tier_of(kind))
            for kind, title, *_ in LAUNCHER_WIZARD
        ),
        *_LEGACY_ROWS,
    ]


def entries(
    *,
    installed_kinds: frozenset[str] = frozenset(),
    query: str = "",
    tier: str | None = None,
) -> list[StoreEntry]:
    """Build the available store entries, optionally filtered by title and tier."""
    unavailable = set(backends.unavailable())
    if not hoyo_visible():
        unavailable.update(HOYO_KINDS)

    needle = query.strip().casefold()
    result = []
    for kind, title, delivery, game_tier in _catalog_rows():
        if kind in unavailable:
            continue
        if tier is not None and game_tier != tier:
            continue
        if needle and needle not in title.casefold():
            continue
        result.append(
            StoreEntry(
                kind=kind,
                title=title,
                tier=game_tier,
                tier_help=TIER_HELP[game_tier],
                delivery_label=delivery,
                size_bytes=backends.install_size_hint(kind),
                has_art=kind in ART or kind in publisher_art_kinds(),
                notice=backends.page_notice(kind),
                installed=kind in installed_kinds,
            )
        )
    result.sort(key=lambda entry: (entry.tier != TIER_VERIFIED, entry.title.casefold()))
    return result


__all__ = ["StoreEntry", "entries"]
