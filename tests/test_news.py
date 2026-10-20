# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the generic news cache/fetcher.

``curl_cffi`` is replaced by an in-memory fake, so no test touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import news


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", payload=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._payload = {} if payload is None else payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRequests:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.routes:
            return self.routes[url]
        return FakeResponse(status_code=404)


class ChunkedResponse(FakeResponse):
    """A response that streams chunks, so the body cap is exercised while reading."""

    def __init__(self, chunks: list[bytes], status_code: int = 200, headers=None):
        super().__init__(
            status_code=status_code, content=b"".join(chunks), headers=headers
        )
        self._chunks = chunks

    def iter_content(self, chunk_size: int = 65536):
        yield from self._chunks


class ClosingResponse(FakeResponse):
    """A response that records whether the caller released the streamed socket."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRequests:
    requests = FakeRequests({})
    monkeypatch.setattr(news, "_curl", lambda: requests)
    return requests


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def test_http_get_json_returns_payload(fake: FakeRequests) -> None:
    fake.routes["https://api.example/x"] = FakeResponse(payload={"code": 0, "value": {"list": []}})
    payload = news.http_get_json("https://api.example/x", headers={"Accept": "application/json"})
    assert payload == {"code": 0, "value": {"list": []}}
    url, kwargs = fake.calls[0]
    assert url == "https://api.example/x"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["User-Agent"].startswith("Nadare/")


def test_http_get_json_rejects_bad_status(fake: FakeRequests) -> None:
    fake.routes["https://api.example/x"] = FakeResponse(status_code=500, content=b"nope")
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")


def test_http_get_json_rejects_oversized_body(fake: FakeRequests) -> None:
    fake.routes["https://api.example/x"] = FakeResponse(
        content=b"x" * (news.MAX_BODY + 1), headers={"Content-Type": "application/json"}
    )
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")


def test_http_get_json_rejects_declared_oversize_before_reading(fake: FakeRequests) -> None:
    """M5: an oversized ``Content-Length`` is refused before the body is read."""
    fake.routes["https://api.example/x"] = FakeResponse(
        content=b"", headers={"Content-Length": str(news.MAX_BODY + 1)}
    )
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")


def test_http_get_json_rejects_oversized_chunked_body(fake: FakeRequests) -> None:
    """M5: a chunked body is aborted once it passes the cap, not buffered whole."""
    chunks = [b"x" * 65536] * (news.MAX_BODY // 65536 + 2)
    fake.routes["https://api.example/x"] = ChunkedResponse(chunks)
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")


def test_http_get_json_rejects_bad_json(fake: FakeRequests) -> None:
    fake.routes["https://api.example/x"] = FakeResponse(content=b"<html>", headers={})
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")


def test_http_get_json_closes_the_streamed_response(fake: FakeRequests) -> None:
    """F9: a ``stream=True`` GET must release its socket, not wait for GC."""
    resp = ClosingResponse(payload={"code": 0})
    fake.routes["https://api.example/x"] = resp
    news.http_get_json("https://api.example/x")
    assert resp.closed is True


def test_http_get_json_closes_the_response_on_error(fake: FakeRequests) -> None:
    """F9: the connection is released even when the fetch raises."""
    resp = ClosingResponse(status_code=500, content=b"nope")
    fake.routes["https://api.example/x"] = resp
    with pytest.raises(news.NewsError):
        news.http_get_json("https://api.example/x")
    assert resp.closed is True


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
def test_normalize_item_clamps_and_requires_fields() -> None:
    raw = {
        "id": 7,
        "title": "t" * 400,
        "summary": "s" * 900,
        "category": "Updates",
        "published_at": 123,
        "image": "https://cdn.example/a.jpg",
        "url": "https://page.example/7",
    }
    item = news.normalize_item(raw)
    assert item is not None
    assert len(item["title"]) == 300
    assert len(item["summary"]) == 600
    assert item["id"] == "7"
    assert item["published_at"] == 123

    assert news.normalize_item({"id": "", "title": "x", "url": "u"}) is None
    assert news.normalize_item({"id": "1", "title": "", "url": "u"}) is None
    assert news.normalize_item({"id": "1", "title": "x", "url": ""}) is None
    assert news.normalize_item("not a dict") is None


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------
def _source(items, partial=False):
    def _call():
        return news.NewsResult(items=list(items), partial=partial)

    return _call


def test_fetch_caches_and_serves_within_ttl(fake: FakeRequests) -> None:
    calls: list[int] = []

    def source():
        calls.append(1)
        return news.NewsResult(items=[{"id": "1", "title": "hi", "url": "https://x/1"}])

    first = news.fetch(source, key="unit", ttl=900)
    assert [i["title"] for i in first.items] == ["hi"]
    assert news.cache_path("unit").is_file()
    second = news.fetch(source, key="unit", ttl=900)
    assert len(calls) == 1
    assert second.items == first.items


def test_fetch_refetches_after_ttl(fake: FakeRequests) -> None:
    calls: list[int] = []

    def source():
        calls.append(1)
        return news.NewsResult(items=[])

    news.fetch(source, key="unit", ttl=0)
    news.fetch(source, key="unit", ttl=0)
    assert len(calls) == 2


def test_fetch_serves_stale_cache_when_offline(fake: FakeRequests) -> None:
    good = news.NewsResult(items=[{"id": "1", "title": "cached", "url": "https://x/1"}])
    news.write_cache("unit", good)

    def boom():
        raise news.NewsError("offline")

    result = news.fetch(boom, key="unit", ttl=0)
    assert result.stale is True
    assert result.items[0]["title"] == "cached"


def test_fetch_raises_when_offline_and_no_cache(fake: FakeRequests) -> None:
    def boom():
        raise news.NewsError("offline")

    with pytest.raises(news.NewsError):
        news.fetch(boom, key="unit", ttl=0)


def test_corrupt_cache_is_ignored(fake: FakeRequests) -> None:
    path = news.cache_path("unit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")

    result = news.fetch(
        _source([{"id": "1", "title": "fresh", "url": "https://x/1"}]), key="unit", ttl=900
    )
    assert result.items[0]["title"] == "fresh"


def test_cache_path_lives_under_cache_dir() -> None:
    assert news.cache_path("czn") == news.paths.cache_dir() / "news" / "czn.json"


def test_cache_path_hashes_hostile_key() -> None:
    """L14: a key with separators must not escape the news cache directory."""
    root = news.paths.cache_dir() / "news"
    for bad in ("../evil", "a/b", "..", "a\\b", "evil\x00"):
        path = news.cache_path(bad)
        assert path.parent == root
        assert ".." not in path.name and "/" not in path.name and "\\" not in path.name


def test_cache_round_trips_non_ascii(tmp_path) -> None:
    """L13: the cache is written and read as UTF-8 regardless of locale."""
    result = news.NewsResult(items=[{"id": "1", "title": "日本語ニュース", "url": "u"}])
    news.write_cache("nonascii", result)
    loaded = news.read_cache("nonascii")
    assert loaded is not None
    assert loaded["items"][0]["title"] == "日本語ニュース"


def test_write_cache_uses_a_unique_part_file(monkeypatch) -> None:
    """R4: concurrent refreshes for one key must not share a ``.part`` file."""
    seen: list[str] = []
    real_replace = news.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(news.os, "replace", spy)
    news.write_cache("uniq", news.NewsResult(items=[]))
    news.write_cache("uniq", news.NewsResult(items=[]))
    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(name.endswith(".part") for name in seen)


def test_credit_string() -> None:
    assert news.CREDIT == "Artwork and news © their publishers. Nadare is not affiliated."


def _item(item_id: str, category: str, published: int, image: str | None = None):
    return {
        "id": item_id,
        "title": f"item {item_id}",
        "summary": "",
        "category": category,
        "published_at": published,
        "image": image,
        "url": f"https://example/{item_id}",
    }


def _hero_calls(monkeypatch) -> list[tuple]:
    from nadare.core import artwork

    calls: list[tuple] = []

    def fake_download(url, slug, kind, hosts):
        calls.append((url, slug, kind, hosts))
        return f"/tmp/{slug}-{kind}"

    monkeypatch.setattr(artwork, "download", fake_download)
    return calls


def test_load_hero_prefers_configured_category(monkeypatch) -> None:
    calls = _hero_calls(monkeypatch)
    items = [
        _item("1", "Promo", 300, "https://cdn.example/promo.png"),
        _item("2", "Updates", 200, "https://cdn.example/update.png"),
        _item("3", "Announcements", 100, "https://cdn.example/ann.png"),
    ]
    result = news.load(
        lambda: news.NewsResult(items=items),
        key="herocat",
        hosts=("cdn.example",),
        slug="herocat",
        ttl=0,
        hero_categories=("Updates", "Announcements"),
    )
    hero_calls = [c for c in calls if c[2] == "official-hero"]
    assert hero_calls == [
        ("https://cdn.example/update.png", "herocat", "official-hero", ("cdn.example",))
    ]
    assert result.hero == "/tmp/herocat-official-hero"


def test_load_hero_falls_back_when_no_category_matches(monkeypatch) -> None:
    calls = _hero_calls(monkeypatch)
    items = [
        _item("1", "Promo", 300, "https://cdn.example/promo.png"),
        _item("2", "Updates", 200, "https://cdn.example/update.png"),
    ]
    news.load(
        lambda: news.NewsResult(items=items),
        key="herofallback",
        hosts=("cdn.example",),
        slug="herofallback",
        ttl=0,
        hero_categories=("Developer notes",),
    )
    hero_calls = [c for c in calls if c[2] == "official-hero"]
    assert hero_calls[0][0] == "https://cdn.example/promo.png"


def test_load_hero_ignores_category_without_image(monkeypatch) -> None:
    calls = _hero_calls(monkeypatch)
    items = [
        _item("1", "Updates", 300, None),
        _item("2", "Promo", 200, "https://cdn.example/promo.png"),
    ]
    news.load(
        lambda: news.NewsResult(items=items),
        key="heroempty",
        hosts=("cdn.example",),
        slug="heroempty",
        ttl=0,
        hero_categories=("Updates",),
    )
    hero_calls = [c for c in calls if c[2] == "official-hero"]
    assert hero_calls[0][0] == "https://cdn.example/promo.png"


def test_load_downloads_thumbnails_and_hero_through_artwork(monkeypatch) -> None:
    """Regression: ``load`` must call the image helper with a valid signature."""
    from nadare.core import artwork

    calls: list[tuple] = []

    def fake_download(url, slug, kind, hosts):
        calls.append((url, slug, kind, hosts))
        return None

    monkeypatch.setattr(artwork, "download", fake_download)
    item = {
        "id": "7",
        "title": "Update",
        "summary": "",
        "category": "Updates",
        "published_at": 1,
        "image": "https://cdn.example/a.png",
        "url": "https://example/7",
    }
    result = news.load(
        lambda: news.NewsResult(items=[item]),
        key="regress",
        hosts=("cdn.example",),
        slug="regress",
        ttl=0,
    )
    assert result.available
    kinds = [c[2] for c in calls]
    assert "thumb" in kinds and "official-hero" in kinds
    assert all(c[3] == ("cdn.example",) for c in calls)


def test_load_never_raises_on_unorderable_hero_key(monkeypatch) -> None:
    """L15: a backend ``hero_key`` returning incomparable values must not escape."""
    from nadare.core import artwork

    monkeypatch.setattr(artwork, "download", lambda *a, **k: None)

    class Unorderable:
        def __lt__(self, other):
            raise TypeError("cannot compare")

        def __gt__(self, other):
            raise TypeError("cannot compare")

    items = [
        _item("1", "Updates", 300, "https://cdn.example/a.png"),
        _item("2", "Updates", 200, "https://cdn.example/b.png"),
    ]
    result = news.load(
        lambda: news.NewsResult(items=items),
        key="badhero",
        hosts=("cdn.example",),
        slug="badhero",
        ttl=0,
        hero_key=lambda _item: Unorderable(),
    )
    assert result.available is True


# ---------------------------------------------------------------------------
# N1: a publisher-controlled item id must never escape the art cache
# ---------------------------------------------------------------------------
def test_load_hashes_hostile_item_id(monkeypatch) -> None:
    """The raw feed id must not be interpolated into the cache filename."""
    from nadare.core import artwork

    calls: list[tuple] = []

    def fake_download(url, slug, kind, hosts):
        calls.append((url, slug, kind, hosts))
        return f"/tmp/{slug}-{kind}"

    monkeypatch.setattr(artwork, "download", fake_download)
    item = _item("../../evil", "Updates", 1, "https://cdn.example/a.png")
    news.load(
        lambda: news.NewsResult(items=[item]),
        key="evilid",
        hosts=("cdn.example",),
        slug="evilid",
        ttl=0,
    )
    thumbs = [c for c in calls if c[2] == "thumb"]
    assert len(thumbs) == 1
    slug = thumbs[0][1]
    assert "../../evil" not in slug
    assert ".." not in slug and "/" not in slug and "\\" not in slug
    assert slug.startswith("evilid-news-")


def test_hostile_news_id_cannot_escape_cache(monkeypatch) -> None:
    """End-to-end: a traversal id stays inside the artwork cache."""
    from nadare.core import artwork

    requests = FakeRequests({})
    requests.routes["https://cdn.example/a.png"] = FakeResponse(
        content=b"IMG", headers={"Content-Type": "image/png"}
    )
    monkeypatch.setattr(artwork, "_curl", lambda: requests)

    item = _item("../../../../evil", "Updates", 1, "https://cdn.example/a.png")
    result = news.load(
        lambda: news.NewsResult(items=[item]),
        key="evilid2",
        hosts=("cdn.example",),
        slug="evilid2",
        ttl=0,
    )
    assert result.available
    root = artwork.cache_dir().resolve()
    paths = [i.get("image_path") for i in result.items if i.get("image_path")]
    assert paths
    for value in paths:
        path = Path(value)
        assert path.parent.resolve() == root
        assert ".." not in path.name and "/" not in path.name
    # No image landed anywhere else in the data home (old code escaped here).
    data_home = artwork.paths.data_dir().parent
    stray = [
        p for p in data_home.rglob("*") if p.is_file() and root not in p.resolve().parents
    ]
    assert stray == []


def test_future_fetched_at_is_stale() -> None:
    now = 1_000_000_000
    assert news._is_fresh({"fetched_at": now - 1000}, 60, now) is True
    assert news._is_fresh({"fetched_at": now + 10_000_000}, 60, now) is False
