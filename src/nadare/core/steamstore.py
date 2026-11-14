# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Steam games as Store tiles.

The Store is keyed on a backend kind, but every Steam game runs on the one ``steam`` backend.
Each tile therefore gets its own kind, ``steam-<app id>``: it is only a Store key, never a
backend, and a tile's profile is an ordinary ``steam`` profile with that app id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import steamsetup

PREFIX = "steam-"
DELIVERY_LABEL = "Through Steam"
TIER_HELP = "Set up was run on a real Steam by a maintainer. Nadare does not install Steam."


@dataclass(frozen=True)
class StoreGame:
    app_id: str
    title: str

    @property
    def kind(self) -> str:
        return kind_for(self.app_id)


def normalise_app_id(value: object) -> str | None:
    """The canonical ASCII-digit app id, or ``None`` when ``value`` is not one.

    Leading zeros are not part of an id (a pasted store URL may carry them), so the backend,
    Store tiles and artwork all key on the same number.
    """
    text = str(value or "")
    if not (text.isascii() and text.isdigit()):
        return None
    return text.lstrip("0") or "0"


def kind_for(app_id: str) -> str:
    return f"{PREFIX}{normalise_app_id(str(app_id).strip()) or app_id}"


def app_id_of(kind: str) -> str | None:
    """The app id a Store kind stands for, or ``None`` when it is not a Steam tile."""
    rest = kind[len(PREFIX) :] if kind.startswith(PREFIX) else ""
    return normalise_app_id(rest)


def store_games() -> list[StoreGame]:
    """Only games whose Set up was seen working on a real Steam are offered."""
    return [StoreGame(game.app_id, game.game) for game in steamsetup.GAMES if game.verified]


def _title_of(kind: str) -> str | None:
    app_id = app_id_of(kind)
    return next((g.title for g in store_games() if g.app_id == app_id), None)


def profile_for(kind: str) -> dict[str, str] | None:
    """The profile an Install on this tile creates, or ``None`` for any other kind."""
    app_id = app_id_of(kind)
    if app_id is None or _title_of(kind) is None:
        return None
    return {"backend": "steam", "steam_app_id": app_id}


def default_name(kind: str) -> str:
    return _title_of(kind) or ""


def _app_id_of_profile(data: Mapping[str, Any]) -> str:
    extra = data.get("extra")
    for source in (data, extra if isinstance(extra, Mapping) else {}):
        for key in ("steam_app_id", "app_id"):
            value = normalise_app_id(str(source.get(key) or "").strip())
            if value is not None:
                return value
    return ""


def installed_kinds(profiles: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """Backend kinds of the profiles, plus the tile kind of each Steam game among them."""
    kinds: set[str] = set()
    for data in profiles.values():
        backend = str(data.get("backend") or "")
        if not backend:
            continue
        kinds.add(backend)
        if backend == "steam" and (app_id := _app_id_of_profile(data)):
            kinds.add(kind_for(app_id))
    return kinds
