# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Fate/Grand Order NA official news source (Aniplex / Lasengle).

The NA webview publishes an Atom feed::

    https://webview.fate-go.us/iframe/web/feed/news_update.xml

Each ``<entry>`` carries a title, an ``<updated>`` date (``MM.DD.YYYY``) and a
link whose query string names the category and the article path::

    http://fate-go.us/news/?category=NEWS&article=/iframe/2026/1004_sugoroku_qbpu/

The newest article pages are fetched in parallel so each item can carry its
event banner (the first ``banner_YYMMDD_*`` image under ``/wp-content/uploads/``).
Only the publisher's own host is ever fetched or accepted for images.

This module is deliberately free of launcher/GUI concerns: it returns plain
items in the :mod:`nadare.core.news` shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

from ...core import download
from ...core.news import NewsError, NewsResult

#: Public Atom feed for NA in-game news.
FEED_URL = "https://webview.fate-go.us/iframe/web/feed/news_update.xml"

#: The static webview host that serves both article pages and banners.
WEBVIEW_HOST = "webview.fate-go.us"
WEBVIEW_BASE = f"https://{WEBVIEW_HOST}"

#: Canonical public news site (the article paths are appended as a query value).
NEWS_SITE = "https://fate-go.us/news/"

#: Only images from the publisher's own webview host are ever downloaded.
IMAGE_HOSTS: tuple[str, ...] = (WEBVIEW_HOST,)

#: How many feed entries to keep (newest first).
MAX_ITEMS = 10
#: How many of the newest article pages to fetch for a banner: every kept item,
#: so an event post low in the list (below routine update notices) keeps its art.
MAX_ARTICLE_FETCH = MAX_ITEMS
#: Parallel article fetches.
_MAX_WORKERS = 4

#: Per-request timeout, matching the shared downloader default.
TIMEOUT = download.DEFAULT_TIMEOUT

_ATOM = "{http://www.w3.org/2005/Atom}"
_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_DATE_FORMATS = ("%m.%d.%Y", "%m/%d/%Y", "%Y-%m-%d")

#: Article paths look like ``/iframe/YYYY/MMDD_slug/``.
_ARTICLE_DATE_RE = re.compile(r"^/iframe/(\d{4})/(\d{2})(\d{2})[_-]")

#: The evergreen tips article is re-dated weekly and must never be the hero.
_HINTS_TITLE_PREFIX = "Fate/Grand Order Hints"


def _text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return str(raw)


def _default_get(url: str) -> bytes:
    return download.http_get(url, timeout=TIMEOUT)


def _absolute(src: str, base: str = WEBVIEW_BASE) -> str | None:
    """Resolve ``src`` against the webview host, rejecting anything off-host."""
    src = src.strip()
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    parsed = urlparse(src)
    if parsed.scheme == "https":
        if parsed.hostname not in IMAGE_HOSTS:
            return None
        if parsed.username or parsed.password or parsed.port is not None:
            return None
        return src
    if parsed.scheme:
        return None
    if not src.startswith("/"):
        src = "/" + src
    return base + src


def banner_url(html: str, base: str = WEBVIEW_BASE) -> str | None:
    """Return the first event-banner image URL in ``html``, or ``None``.

    A banner is an ``<img>`` whose path lives under ``/wp-content/uploads/`` and
    whose file name starts with ``banner_`` (``.png``/``.jpg``/``.jpeg``). Only
    the publisher's own host is accepted; anything else is skipped.
    """
    for match in _IMG_RE.finditer(html):
        src = match.group(1).strip()
        if "/wp-content/uploads/" not in src:
            continue
        name = src.split("?", 1)[0].rsplit("/", 1)[-1].lower()
        if not name.startswith("banner_") or not name.endswith((".png", ".jpg", ".jpeg")):
            continue
        resolved = _absolute(src, base)
        if resolved:
            return resolved
    return None


def _published_at(value: str) -> int:
    """``MM.DD.YYYY`` -> epoch milliseconds (UTC); 0 when unparseable."""
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return int(parsed.timestamp() * 1000)
    return 0


def article_date_ms(article: str) -> int:
    """Epoch milliseconds encoded in ``/iframe/YYYY/MMDD_slug/``; 0 when absent.

    The evergreen tips path (``/iframe/2023/0_tips/``) has no valid ``MMDD`` and
yields 0, so it can never outrank a real dated event.
    """
    match = _ARTICLE_DATE_RE.match(article.strip())
    if not match:
        return 0
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1000)


def hero_key(item: dict[str, Any]) -> int | None:
    """Rank ``item`` for the hero by its article's own date, or ``None``.

    The newer ``<updated>`` feed date is ignored: the publisher re-dates the
evergreen tips page every week, so ordering by it would always pick tips.
    Only paths matching ``/iframe/YYYY/MMDD_slug/`` with a banner qualify.
    """
    title = str(item.get("title") or "")
    if title.startswith(_HINTS_TITLE_PREFIX):
        return None
    article = (parse_qs(urlparse(str(item.get("url") or "")).query).get("article") or [""])[0]
    date = article_date_ms(article)
    return date or None


def _slug(article: str) -> str:
    path = article.strip("/")
    if path.startswith("iframe/"):
        path = path[len("iframe/") :]
    return path.replace("/", "-")


def _safe_article_path(article: str) -> bool:
    """True when ``WEBVIEW_BASE + article`` stays on the publisher's host.

    The value comes from the feed. Anything that is not a plain absolute path
    (``@evil.host`` userinfo, ``//host``, backslashes, ``..`` or control
    characters) is rejected, not repaired.
    """
    return (
        article.startswith("/")
        and not article.startswith("//")
        and "@" not in article
        and "\\" not in article
        and ".." not in article
        and all(ch.isprintable() for ch in article)
    )


def _entry(entry: ElementTree.Element) -> tuple[dict[str, Any], str] | None:
    """Map one Atom ``<entry>`` to ``(item, article_path)``, or ``None``."""
    title = (entry.findtext(f"{_ATOM}title") or "").strip()
    updated = (entry.findtext(f"{_ATOM}updated") or "").strip()
    href = ""
    for link in entry.findall(f"{_ATOM}link"):
        candidate = str(link.get("href") or "").strip()
        if candidate:
            href = candidate
            break
    if not title or not href:
        return None

    query = parse_qs(urlparse(href).query)
    category = (query.get("category") or [""])[0].strip()
    article = (query.get("article") or [""])[0].strip()
    item_id = _slug(article)
    if not article or not item_id or not _safe_article_path(article):
        return None

    # The feed decodes these from its own query string; re-encode so a value
    # with ``&``/``%``/spaces cannot corrupt the public URL. Slashes are kept in
    # the article path (it is a path value, not an opaque token).
    params = []
    if category:
        params.append("category=" + quote(category, safe=""))
    params.append("article=" + quote(article, safe="/"))
    public_url = NEWS_SITE + "?" + "&".join(params)

    item = {
        "id": item_id,
        "title": title,
        "summary": "",
        "category": category.title(),
        "published_at": _published_at(updated),
        "image": None,
        "url": public_url,
    }
    return item, article


def fetch_fgo_news(get: Callable[[str], Any] | None = None) -> NewsResult:
    """Return newest-first FGO NA news with event banners; raise on a bad feed."""
    getter = get or _default_get
    try:
        raw = getter(FEED_URL)
    except Exception as exc:  # noqa: BLE001 - network failures become NewsError
        raise NewsError(str(exc)) from exc

    try:
        root = ElementTree.fromstring(_text(raw))
    except ElementTree.ParseError as exc:
        raise NewsError(f"invalid FGO news feed: {exc}") from exc

    collected: list[dict[str, Any]] = []
    articles: list[str] = []
    seen: set[str] = set()
    for entry in root.findall(f"{_ATOM}entry"):
        parsed = _entry(entry)
        if parsed is None:
            continue
        item, article = parsed
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        collected.append(item)
        articles.append(article)
        if len(collected) >= MAX_ITEMS:
            break

    partial = False
    indexes = list(range(min(len(collected), MAX_ARTICLE_FETCH)))
    if indexes:
        partial = _attach_banners(collected, articles, indexes, getter)

    return NewsResult(items=collected, partial=partial)


def _attach_banners(
    items: list[dict[str, Any]],
    articles: list[str],
    indexes: list[int],
    getter: Callable[[str], Any],
) -> bool:
    """Fetch article pages in parallel and attach banner URLs; True if any failed."""

    def work(index: int) -> tuple[int, str | None]:
        page = getter(WEBVIEW_BASE + articles[index])
        return index, banner_url(_text(page))

    partial = False
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(indexes))) as pool:
        for future in [pool.submit(work, index) for index in indexes]:
            try:
                index, image = future.result()
            except Exception:  # noqa: BLE001 - a failed article keeps the item
                partial = True
                continue
            if image:
                items[index]["image"] = image
    return partial


__all__ = [
    "FEED_URL",
    "IMAGE_HOSTS",
    "MAX_ARTICLE_FETCH",
    "MAX_ITEMS",
    "NEWS_SITE",
    "WEBVIEW_BASE",
    "WEBVIEW_HOST",
    "article_date_ms",
    "banner_url",
    "fetch_fgo_news",
    "hero_key",
]
