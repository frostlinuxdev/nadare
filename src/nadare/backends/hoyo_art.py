# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""HoYoverse launcher art: key visual and card image for Genshin, Star Rail, ZZZ and HI3.

The launcher's public games endpoint lists every game with its current artwork::

    GET https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGames
        ?launcher_id=VYTpXlbWo8&language=en-us

``display.background`` is the wide key visual (hero) and ``display.thumbnail`` a small card
image (cover). Only plain https URLs on the publisher's image CDNs are accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core.news import NewsError, http_get_json, https_on
from .hoyo_news import GAME_IDS

API = "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGames"
LAUNCHER_ID = "VYTpXlbWo8"
IMAGE_HOSTS: tuple[str, ...] = ("fastcdn.hoyoverse.com", "launcher-webstatic.hoyoverse.com")

#: Art kind -> field of the game's ``display`` object.
_FIELDS = {"hero": "background", "cover": "thumbnail"}


def _image(value: Any) -> str | None:
    url = value.get("url") if isinstance(value, dict) else None
    return https_on(url, IMAGE_HOSTS)


def fetch_art(game_id: str, get_json: Callable[..., Any] | None = None) -> dict[str, str]:
    """Return ``{"hero": url, "cover": url}`` for ``game_id`` (missing kinds are left out).

    Raises :class:`NewsError` on a bad response envelope.
    """
    get = get_json or http_get_json
    payload = get(f"{API}?launcher_id={LAUNCHER_ID}&language=en-us")
    data = payload.get("data") if isinstance(payload, dict) else None
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or payload.get("retcode") != 0 or not isinstance(games, list):
        raise NewsError("bad HoYoverse games envelope")
    for game in games:
        if not isinstance(game, dict) or game.get("id") != game_id:
            continue
        display = game.get("display")
        if not isinstance(display, dict):
            return {}
        found = {kind: _image(display.get(field)) for kind, field in _FIELDS.items()}
        return {kind: url for kind, url in found.items() if url}
    return {}


def fetch_genshin_art(get_json: Callable[..., Any] | None = None) -> dict[str, str]:
    return fetch_art(GAME_IDS["genshin"], get_json)


def fetch_hsr_art(get_json: Callable[..., Any] | None = None) -> dict[str, str]:
    return fetch_art(GAME_IDS["hsr"], get_json)


def fetch_zzz_art(get_json: Callable[..., Any] | None = None) -> dict[str, str]:
    return fetch_art(GAME_IDS["zzz"], get_json)


def fetch_hi3_art(get_json: Callable[..., Any] | None = None) -> dict[str, str]:
    return fetch_art(GAME_IDS["hi3"], get_json)
