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
    #: Name of the game when it has several builds (``""`` for a game with one).
    group: str = ""
    #: What this build is, shown in the version picker (``""`` for a game with one build).
    variant: str = ""


#: Builds of one game that the catalog lists separately: kind -> (game, what this build is).
_VARIANTS: dict[str, tuple[str, str]] = {
    "direct-gfl2": ("Girls' Frontline 2", "Exilium, Darkwinter build"),
    "direct-gfl2-haoplay": ("Girls' Frontline 2", "Haoplay global build"),
    "launcher-gfl2-haoplay": (
        "Girls' Frontline 2",
        "Haoplay global build, through the publisher's launcher",
    ),
    "direct-nte": ("Neverness to Everness", "Direct download"),
    "launcher-nte": ("Neverness to Everness", "Through the publisher's launcher"),
}


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
        group, variant = _VARIANTS.get(kind, ("", ""))
        if needle and needle not in title.casefold() and needle not in group.casefold():
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
                group=group,
                variant=variant,
            )
        )
    result.sort(key=lambda entry: (entry.tier != TIER_VERIFIED, entry.title.casefold()))
    return result


@dataclass(frozen=True)
class StoreGame:
    """One game as the Store shows it: all of its builds, the most supported one first."""

    title: str
    entries: tuple[StoreEntry, ...]

    @property
    def primary(self) -> StoreEntry:
        return self.entries[0]

    @property
    def installed(self) -> bool:
        return any(entry.installed for entry in self.entries)


def store_games(
    *,
    installed_kinds: frozenset[str] = frozenset(),
    query: str = "",
    tier: str | None = None,
) -> list[StoreGame]:
    """:func:`entries` with the builds of one game gathered into a single game."""
    games: list[StoreGame] = []
    index: dict[str, int] = {}
    for entry in entries(installed_kinds=installed_kinds, query=query, tier=tier):
        if not entry.group:
            games.append(StoreGame(entry.title, (entry,)))
        elif entry.group in index:
            at = index[entry.group]
            games[at] = StoreGame(entry.group, (*games[at].entries, entry))
        else:
            index[entry.group] = len(games)
            games.append(StoreGame(entry.group, (entry,)))
    return games


def game_of(kind: str, *, installed_kinds: frozenset[str] = frozenset()) -> StoreGame | None:
    """The game ``kind`` is a build of, with every build (not narrowed by any filter)."""
    for game in store_games(installed_kinds=installed_kinds):
        if any(entry.kind == kind for entry in game.entries):
            return game
    return None


__all__ = ["StoreEntry", "StoreGame", "entries", "game_of", "store_games"]
