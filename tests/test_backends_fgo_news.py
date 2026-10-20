# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the Fate/Grand Order NA official news source.

The feed and article HTTP calls are injected, so nothing here opens a socket.
"""

from __future__ import annotations

from urllib.parse import quote

from nadare.backends.fgo import news

SAMPLE_ARTICLE = "/iframe/2026/1004_sugoroku_qbpu/"


def _entry(title: str, updated: str, category: str, article: str) -> str:
    href = f"http://fate-go.us/news/?category={category}&article={article}"
    return (
        "<entry>"
        f"<title>{title}</title>"
        f"<updated>{updated}</updated>"
        f'<link href="{href.replace("&", "&amp;")}"/>'
        "</entry>"
    )


def _feed(entries: list[str]) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"
    ).encode()


_BANNERS = {
    f"{news.WEBVIEW_BASE}{SAMPLE_ARTICLE}": (
        b"<html><body>"
        b'<img src="/wp-content/uploads/2026/10/banner_261004_event.png" alt="">'
        b'<img src="/wp-content/uploads/2026/10/banner_261099_later.png">'
        b"</body></html>"
    )
}


def _get(url: str) -> bytes:
    if url == news.FEED_URL:
        return _feed([_entry("Sugoroku Quest", "10.04.2026", "NEWS", SAMPLE_ARTICLE)])
    if url in _BANNERS:
        return _BANNERS[url]
    return b"<html></html>"


def test_fetch_maps_atom_fields() -> None:
    result = news.fetch_fgo_news(get=_get)

    assert result.partial is False
    assert len(result.items) == 1
    item = result.items[0]
    assert item["id"] == "2026-1004_sugoroku_qbpu"
    assert item["title"] == "Sugoroku Quest"
    assert item["category"] == "News"
    assert item["published_at"] > 0
    assert item["url"] == (
        "https://fate-go.us/news/?category=NEWS&article=/iframe/2026/1004_sugoroku_qbpu/"
    )
    assert item["image"] == (
        "https://webview.fate-go.us/wp-content/uploads/2026/10/banner_261004_event.png"
    )


def test_first_banner_wins_and_jpg_is_accepted() -> None:
    article = "/iframe/2026/1001_jpg/"
    feed = _feed([_entry("JPG event", "10.01.2026", "UPDATES", article)])
    page = (
        b'<img src="/wp-content/uploads/2026/10/banner_261001_a.jpg">'
        b'<img src="/wp-content/uploads/2026/10/banner_261001_b.png">'
    )

    def get(url: str) -> bytes:
        if url == news.FEED_URL:
            return feed
        if url == f"{news.WEBVIEW_BASE}{article}":
            return page
        return b"<html></html>"

    result = news.fetch_fgo_news(get=get)
    assert result.items[0]["category"] == "Updates"
    assert result.items[0]["image"] == (
        "https://webview.fate-go.us/wp-content/uploads/2026/10/banner_261001_a.jpg"
    )


def test_failed_article_keeps_item_without_image() -> None:
    good = "/iframe/2026/1001_good/"
    bad = "/iframe/2026/1002_bad/"
    feed = _feed(
        [
            _entry("Bad", "10.02.2026", "NEWS", bad),
            _entry("Good", "10.01.2026", "NEWS", good),
        ]
    )

    def get(url: str) -> bytes:
        if url == news.FEED_URL:
            return feed
        if url == f"{news.WEBVIEW_BASE}{good}":
            return b'<img src="/wp-content/uploads/2026/10/banner_261001_x.png">'
        raise RuntimeError("boom")

    result = news.fetch_fgo_news(get=get)
    by_id = {item["id"]: item for item in result.items}
    assert by_id["2026-1002_bad"]["image"] is None
    assert by_id["2026-1001_good"]["image"] is not None
    assert result.partial is True


def test_every_kept_item_gets_its_banner() -> None:
    entries = [
        _entry(f"Event {i}", f"10.{i:02d}.2026", "NEWS", f"/iframe/2026/10{i:02d}_e/")
        for i in range(1, 11)
    ]
    fetched: list[str] = []

    def get(url: str) -> bytes:
        if url == news.FEED_URL:
            return _feed(entries)
        fetched.append(url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return f'<img src="/wp-content/uploads/2026/10/{slug}/banner_x.png">'.encode()

    result = news.fetch_fgo_news(get=get)
    assert len(result.items) == news.MAX_ITEMS
    assert len(fetched) == news.MAX_ITEMS
    # The oldest kept items (the main event post sits here in practice) get art too.
    assert all(item["image"] for item in result.items)


def test_malformed_feed_raises() -> None:
    import pytest

    from nadare.core.news import NewsError

    def get(url: str) -> bytes:
        return b"not xml at all"

    with pytest.raises(NewsError):
        news.fetch_fgo_news(get=get)


def test_feed_values_are_reencoded_in_the_public_url() -> None:
    """L5: query values are re-encoded so ``&``/spaces cannot corrupt the URL."""
    category = "News & Notes"
    article = "/iframe/2026/1004_a&b c/"
    href = (
        "http://fate-go.us/news/?category="
        + quote(category, safe="")
        + "&article="
        + quote(article, safe="")
    )
    feed = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>T</title><updated>10.04.2026</updated>"
        f'<link href="{href.replace("&", "&amp;")}"/></entry></feed>'
    ).encode()

    result = news.fetch_fgo_news(get=lambda url: feed)

    item = result.items[0]
    assert " " not in item["url"]
    assert "category=News%20%26%20Notes" in item["url"]
    # Article slashes stay literal (it is a path), but ``&``/space are encoded.
    assert "article=/iframe/2026/1004_a%26b%20c/" in item["url"]
    assert item["url"].count("&") == 1, "only the parameter separator may be literal"


def test_entry_without_link_is_dropped() -> None:
    feed = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        b"<entry><title>No link</title><updated>10.01.2026</updated></entry>"
        b"</feed>"
    )

    def get(url: str) -> bytes:
        return feed

    result = news.fetch_fgo_news(get=get)
    assert result.items == []


def test_banner_url_rejects_offhost_images() -> None:
    assert news.banner_url('<img src="https://evil.example/banner_1.png">') is None
    assert news.banner_url('<img src="https://webview.fate-go.us/banner_1.png">') is None
    assert (
        news.banner_url('<img src="/wp-content/uploads/2026/10/banner_261004_x.png">')
        == "https://webview.fate-go.us/wp-content/uploads/2026/10/banner_261004_x.png"
    )


def test_backend_wires_the_source_and_hosts() -> None:
    from nadare.backends.fgo.backend import FgoLeptonBackend

    assert FgoLeptonBackend.news_source is news.fetch_fgo_news
    assert FgoLeptonBackend.official_image_hosts == news.IMAGE_HOSTS
    assert FgoLeptonBackend.news_hero_categories == ("News",)
    assert FgoLeptonBackend.news_hero_key is news.hero_key


def test_article_date_ms_parses_only_dated_paths() -> None:
    assert news.article_date_ms("/iframe/2026/1004_sugoroku_qbpu/") > 0
    assert news.article_date_ms("/iframe/2023/0_tips/") == 0
    assert news.article_date_ms("/iframe/2026/9999_bad/") == 0
    assert news.article_date_ms("not-an-article") == 0


def _hero_item(title: str, article: str, image: str | None = "https://x/banner.png") -> dict:
    return {
        "id": news.article_date_ms(article),
        "title": title,
        "summary": "",
        "category": "News",
        "published_at": 1,
        "image": image,
        "url": f"https://fate-go.us/news/?category=NEWS&article={article}",
    }


def test_hero_key_skips_the_evergreen_tips_page() -> None:
    tips = _hero_item(
        "Fate/Grand Order Hints and Strategies", "/iframe/2023/0_tips/"
    )
    assert news.hero_key(tips) is None
    # A dated article wins; an undated one is ignored even without the title.
    assert news.hero_key(_hero_item("Event", "/iframe/2026/1004_event/")) is not None
    assert news.hero_key(_hero_item("Odd", "/iframe/2023/0_tips/")) is None


def test_hero_is_the_newest_event_not_the_re_dated_tips() -> None:
    """Today's feed: tips is first (weekly ``<updated>``) but the event must win."""
    from nadare.core import news as core_news

    tips = _hero_item("Fate/Grand Order Hints and Strategies", "/iframe/2023/0_tips/")
    newest = _hero_item("Road to 7", "/iframe/2026/1004_road_to_seven/")
    older = _hero_item("Summer event", "/iframe/2026/1001_summer/")
    items = [tips, newest, older]

    index = core_news._hero_index(items, ("News",), news.hero_key)

    assert items[index]["title"] == "Road to 7"
    assert [item["title"] for item in items] == [
        "Fate/Grand Order Hints and Strategies",
        "Road to 7",
        "Summer event",
    ], "the feed order must be preserved"


def test_load_uses_the_hero_key(monkeypatch) -> None:
    from nadare.core import artwork
    from nadare.core import news as core_news

    calls: list[str] = []

    def fake_download(url, slug, kind, hosts):
        calls.append(url)
        return f"/tmp/{slug}-{kind}"

    monkeypatch.setattr(artwork, "download", fake_download)
    tips = _hero_item("Fate/Grand Order Hints and Strategies", "/iframe/2023/0_tips/")
    newest = _hero_item("Road to 7", "/iframe/2026/1004_road_to_seven/")
    result = core_news.load(
        lambda: core_news.NewsResult(items=[tips, newest]),
        key="fgo-test",
        hosts=("webview.fate-go.us",),
        slug="fgo-test",
        ttl=0,
        hero_categories=("News",),
        hero_key=news.hero_key,
    )
    assert result.hero == "/tmp/fgo-test-official-hero"
    assert calls[-1] == newest["image"]

