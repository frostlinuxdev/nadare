# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Mongil: Star Dive news from the official Netmarble forum's public JSON API.

The game site has no news of its own; its news lives on ``forum.netmarble.com/stardive_gl``.
Each board (2 Notice, 8 Update, 6 Events) is one anonymous request returning
``{code: 0, articleList: [{id, menuSeq, title, regDate, thumbnailUrl, managerLanguageCds}]}``.
A list mixes the Thai, Traditional Chinese and English posts, so only rows whose
``managerLanguageCds`` include ``en_US`` are kept. Boards are merged, deduplicated and sorted
newest first; one failing board makes the result partial, all failing is an error.
Thumbnails are limited to the forum's image CDN. This module is free of launcher/GUI concerns:
it returns plain items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_json, https_on

MAX_ITEMS = 12
_API = "https://forum.netmarble.com/api/game/monster2/official/forum/stardive_gl/article/list"
ARTICLE = "https://forum.netmarble.com/stardive_gl/view/{board}/{id}"
IMAGE_HOSTS: tuple[str, ...] = ("hedwig-cf.netmarble.com",)
LANGUAGE = "en_US"
BOARDS: dict[int, str] = {2: "Notice", 8: "Update", 6: "Events"}

_ID_RE = re.compile(r"\A[0-9]{1,12}\Z")


def board_url(board: int) -> str:
    return f"{_API}?menuSeq={board}&viewType=lv&rows={MAX_ITEMS}&start=0&languageCd={LANGUAGE}"


def _news_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.match(text) else None


def _published_at(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _rows(payload: Any) -> list[Any]:
    rows = payload.get("articleList") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(rows, list):
        raise NewsError("bad Mongil news envelope")
    return rows


def _english(raw: dict[str, Any]) -> bool:
    languages = raw.get("managerLanguageCds")
    return isinstance(languages, list) and LANGUAGE in languages


def fetch_mongil_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Mongil news; one bad board makes the result partial."""
    get = get_json or http_get_json
    items: list[dict[str, Any]] = []
    failures = 0
    for board, category in BOARDS.items():
        try:
            rows = _rows(get(board_url(board), headers={"Accept": "application/json"}))
        except Exception:  # noqa: BLE001 - one dead board is partial, not fatal
            failures += 1
            continue
        for raw in rows:
            if not isinstance(raw, dict) or not _english(raw):
                continue
            news_id = _news_id(raw.get("id"))
            title = str(raw.get("title") or "").strip()
            if not news_id or not title:
                continue
            items.append(
                {
                    "id": news_id,
                    "title": title,
                    "summary": "",
                    "category": category,
                    "published_at": _published_at(raw.get("regDate")),
                    "image": https_on(raw.get("thumbnailUrl"), IMAGE_HOSTS),
                    "url": ARTICLE.format(board=board, id=news_id),
                }
            )

    if failures == len(BOARDS):
        raise NewsError("all Mongil news boards failed")
    items.sort(key=lambda item: item["published_at"], reverse=True)

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    return NewsResult(items=unique[:MAX_ITEMS], partial=failures > 0)


__all__ = ["ARTICLE", "BOARDS", "IMAGE_HOSTS", "MAX_ITEMS", "board_url", "fetch_mongil_news"]
