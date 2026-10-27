# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""NIKKE official news from the site's unsigned CMS POST.

The CMS answers ``GetContentByLabel`` with ``{code: 0, data: {info_content: [...]}}``;
each row carries ``content_id``, ``title``, ``pub_timestamp`` (unix seconds, as a
string) and ``pic_urls``. Only the publisher's player-infinite CDNs are accepted
for images. This module is free of launcher/GUI concerns: it returns plain items
in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.news import NewsError, NewsResult, http_post_json, https_on

API = (
    "https://na-community.playerinfinite.com/api/gpts.information_feeds_svr."
    "InformationFeedsSvr/GetContentByLabel"
)
ARTICLE = "https://nikke-en.com/newsdetail.html?content_id={id}"
IMAGE_HOSTS: tuple[str, ...] = (
    "na-nikke-aws.playerinfinite.com",
    "na-community.playerinfinite.com",
)
HEADERS = {
    "X-GameId": "16",
    "X-AreaId": "na",
    "X-Source": "pc_web",
    "X-Language": "en",
}
BODY = {
    "get_num": 12,
    "offset": 0,
    "primary_label_id": 309,
    "secondary_label_id": 496,
    "content_class": 0,
}
MAX_ITEMS = 12

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


def _image(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for candidate in value:
        url = https_on(candidate, IMAGE_HOSTS)
        if url:
            return url
    return None


def fetch_nikke_news(post_json: Callable[..., Any] | None = None) -> NewsResult:
    """Return newest-first NIKKE news; ``NewsError`` on a bad envelope."""
    post = post_json or http_post_json
    payload = post(API, dict(BODY), headers=dict(HEADERS))
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("info_content") if isinstance(data, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("code") != 0
        or not isinstance(rows, list)
    ):
        raise NewsError("bad NIKKE news envelope")

    items: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        content_id = _news_id(raw.get("content_id"))
        title = str(raw.get("title") or "").strip()
        if not content_id or not title:
            continue
        items.append(
            {
                "id": content_id,
                "title": title,
                "summary": str(raw.get("content_part") or "").strip(),
                "category": "News",
                "published_at": _published_at(raw.get("pub_timestamp")),
                "image": _image(raw.get("pic_urls")),
                "url": ARTICLE.format(id=content_id),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)

    seen: set[str] = set()
    unique = [item for item in items if not (item["id"] in seen or seen.add(item["id"]))]
    return NewsResult(items=unique[:MAX_ITEMS])


__all__ = [
    "API",
    "ARTICLE",
    "BODY",
    "HEADERS",
    "IMAGE_HOSTS",
    "MAX_ITEMS",
    "fetch_nikke_news",
]
