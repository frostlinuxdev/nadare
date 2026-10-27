# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Arknights: Endfield official bulletins from the public web-news API.

Each tab (``notices``, ``events``, ``news``) is a separate request returning
``{code: 0, data: {list: [{cid, tab, title, displayTime, cover, brief}]}}``.
The tabs are merged, deduplicated by ``cid`` and sorted by ``displayTime``. A
single failing tab marks the result partial; only when every tab fails is the
feed an error. Covers are limited to the publisher's static CDN. This module is
free of launcher/GUI concerns: it returns plain items in the
:mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_json, https_on

API = "https://web-news.gryphline.com/api/bulletin"
QUERY = "lang=en-us&code=arknights_endfield_official&page=1&pageSize=8"
TABS: tuple[str, ...] = ("notices", "events", "news")
ARTICLE = "https://endfield.gryphline.com/en-us/news/{id}"
IMAGE_HOSTS: tuple[str, ...] = ("web-static.hg-cdn.com",)
MAX_ITEMS = 12

_CATEGORIES = {
    "notices": "Notices",
    "events": "Events",
    "news": "News",
}
_ID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _news_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.match(text) else None


def _published_at(value: Any) -> int:
    try:
        return int(float(str(value).strip()) * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _tab_url(tab: str) -> str:
    return f"{API}?{QUERY}&tabs[]={tab}"


def _rows(payload: Any) -> list[Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("list") if isinstance(data, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("code") != 0
        or not isinstance(rows, list)
    ):
        raise NewsError("bad Endfield news envelope")
    return rows


def fetch_endfield_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Endfield news; one bad tab makes the result partial."""
    get = get_json or http_get_json
    items: list[dict[str, Any]] = []
    failures = 0
    for tab in TABS:
        try:
            rows = _rows(get(_tab_url(tab), headers={"Accept": "application/json"}))
        except Exception:  # noqa: BLE001 - one dead tab is partial, not fatal
            failures += 1
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            news_id = _news_id(raw.get("cid"))
            title = str(raw.get("title") or "").strip()
            if not news_id or not title:
                continue
            items.append(
                {
                    "id": news_id,
                    "title": title,
                    "summary": str(raw.get("brief") or "").strip(),
                    "category": _CATEGORIES.get(tab, tab.title()),
                    "published_at": _published_at(raw.get("displayTime")),
                    "image": https_on(raw.get("cover"), IMAGE_HOSTS),
                    "url": ARTICLE.format(id=news_id),
                }
            )

    if failures == len(TABS):
        raise NewsError("all Endfield news tabs failed")
    items.sort(key=lambda item: item["published_at"], reverse=True)

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    return NewsResult(items=unique[:MAX_ITEMS], partial=failures > 0)


__all__ = [
    "API",
    "ARTICLE",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "QUERY",
    "TABS",
    "fetch_endfield_news",
]
