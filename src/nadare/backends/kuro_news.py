# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Kuro Games official news: Wuthering Waves and Punishing: Gray Raven.

Both sites publish a static ``ArticleMenu.json`` array (``articleId``,
``articleTitle``, ``articleType``, ``startTime``) and a ``MainMenu.json`` that
maps article types to their localised labels and carries the hero cover. The
list is not sorted, so items are ordered by ``startTime`` (CMS time, treated as
UTC) before being returned.

Only the publisher's two media CDNs are accepted for images. This module is free
of launcher/GUI concerns: it returns plain items in the
:mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..core.news import NewsError, NewsResult, http_get_json, https_on

WUWA_LIST = (
    "https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/en/ArticleMenu.json"
)
WUWA_MAIN = (
    "https://hw-media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/en/MainMenu.json"
)
PGR_LIST = (
    "https://media-cdn-zspms.kurogame.net/pnswebsite/website2.0/json/G167/ArticleMenu.json"
)
PGR_MAIN = (
    "https://media-cdn-zspms.kurogame.net/pnswebsite/website2.0/json/G167/MainMenu.json"
)

WUWA_ARTICLE = "https://wutheringwaves.kurogames.com/en/main/news/detail/{id}"
WUWA_SITE = "https://wutheringwaves.kurogames.com/en/main/news"
PGR_ARTICLE = "https://pgr.kurogame.net/news/{id}"
PGR_SITE = "https://pgr.kurogame.net/news"

IMAGE_HOSTS: tuple[str, ...] = (
    "hw-media-cdn-mingchao.kurogame.com",
    "media-cdn-zspms.kurogame.net",
)

MAX_ITEMS = 12

_ID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _news_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _ID_RE.match(text) else None


def _published_at(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1000)


def _main_menu(payload: Any) -> tuple[dict[str, str], str | None]:
    """Return the article-type labels and the allowlisted hero cover."""
    if not isinstance(payload, dict):
        return {}, None
    labels: dict[str, str] = {}
    rows = payload.get("articleType")
    if isinstance(rows, list):
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            key = raw.get("contentId")
            label = str(raw.get("contentLabel") or "").strip()
            if key is not None and label:
                labels[str(key)] = label
    top = payload.get("pcTopPicture")
    cover = https_on(top.get("coverImage") if isinstance(top, dict) else None, IMAGE_HOSTS)
    return labels, cover


def _fetch(
    list_url: str,
    main_url: str,
    article_url: str,
    site_url: str,
    get_json: Callable[..., Any] | None,
) -> NewsResult:
    get = get_json or http_get_json

    labels: dict[str, str] = {}
    cover: str | None = None
    try:
        labels, cover = _main_menu(get(main_url, headers={"Accept": "application/json"}))
    except Exception:  # noqa: BLE001 - a dead main menu must not fail the feed
        labels, cover = {}, None

    payload = get(list_url, headers={"Accept": "application/json"})
    if not isinstance(payload, list):
        raise NewsError("bad Kuro news envelope")

    items: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        article_id = _news_id(raw.get("articleId"))
        title = str(raw.get("articleTitle") or "").strip()
        if not article_id or not title:
            continue
        items.append(
            {
                "id": article_id,
                "title": title,
                "summary": "",
                "category": labels.get(str(raw.get("articleType")), ""),
                "published_at": _published_at(raw.get("startTime")),
                "image": None,
                "url": article_url.format(id=article_id),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)
    seen: set[str] = set()
    items = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    items = items[: MAX_ITEMS - 1 if cover else MAX_ITEMS]

    if cover:
        items.append(
            {
                "id": "featured",
                "title": "Featured",
                "summary": "",
                "category": "Featured",
                "published_at": 0,
                "image": cover,
                "url": site_url,
            }
        )

    return NewsResult(items=items)


def fetch_wuwa_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Wuthering Waves news."""
    return _fetch(WUWA_LIST, WUWA_MAIN, WUWA_ARTICLE, WUWA_SITE, get_json)


def fetch_pgr_news(get_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first Punishing: Gray Raven news."""
    return _fetch(PGR_LIST, PGR_MAIN, PGR_ARTICLE, PGR_SITE, get_json)


__all__ = [
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "PGR_ARTICLE",
    "PGR_LIST",
    "PGR_MAIN",
    "PGR_SITE",
    "WUWA_ARTICLE",
    "WUWA_LIST",
    "WUWA_MAIN",
    "WUWA_SITE",
    "fetch_pgr_news",
    "fetch_wuwa_news",
]
