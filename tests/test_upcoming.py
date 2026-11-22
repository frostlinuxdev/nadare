# SPDX-License-Identifier: GPL-3.0-or-later
"""Coming-soon feed contracts; every fetch is injected."""
from __future__ import annotations

import json
from pathlib import Path

from nadare.core import artindex, paths, upcoming

URL = "https://index.example/catalog/upcoming.json"
ART = "https://cdn2.unrealengine.com/sample.jpg"
ALLOWED_ART_HOSTS = sorted({host for hosts in artindex.ALLOWED_HOSTS.values() for host in hosts})


def row(**changes):
    return {
        "title": "Silver Palace",
        "publisher": "Silver Studio",
        "when": "Q1 2027",
        "link": "https://silver.example/",
        **changes,
    }


def test_sample_is_parsed_and_text_is_cleaned():
    sample = json.loads((Path(__file__).parent / "fixtures/upcoming.json").read_text())
    items = upcoming.parse(sample)
    assert [(item.title, item.publisher, item.when) for item in items] == [
        ("Silver Palace", "Silver Studio", "Q1 2027"),
        ("Star Garden", "Garden Studio", "2027"),
        ("Moon Voyage", "Moon Studio", "To be announced"),
    ]
    assert items[0].art == ART
    assert items[0].link == "https://silver.example/"
    assert items[1].art is None
    [item] = upcoming.parse({"upcoming": [row(title=" A\nB\x00 " + "x" * 400)]})
    assert len(item.title) == upcoming.MAX_TEXT
    assert "\n" not in item.title and "\x00" not in item.title


def test_bad_entries_are_skipped_and_the_card_count_is_capped():
    bad = [
        None,
        {},
        7,
        dict(row(), title=4),
        dict(row(), publisher="   "),
        dict(row(), when=[]),
        dict(row(), title="\x00"),
    ]
    assert upcoming.parse({"upcoming": [*bad, row()]}) == [upcoming.Upcoming(**row())]
    rows = [dict(row(), title=f"Game {i}") for i in range(20)]
    assert len(upcoming.parse({"upcoming": rows})) == 8 == upcoming.MAX_CARDS
    assert len(upcoming.parse({"upcoming": [row(), dict(row(), publisher="Other")]})) == 1
    assert upcoming.parse({"upcoming": "wrong"}) == []
    assert upcoming.parse(None) == []


def test_link_is_optional_and_a_bad_link_only_drops_the_button():
    [item] = upcoming.parse({"upcoming": [dict(row(), link=None)]})
    assert item.title == "Silver Palace" and item.link is None
    for link in (
        "",
        "http://silver.example/",
        "file:///tmp/x",
        "javascript:alert(1)",
        "https://u:p@silver.example/",
        "https://silver.example:443/",
        "https://[broken",
        " https://silver.example/",
    ):
        [item] = upcoming.parse({"upcoming": [row(link=link)]})
        assert item.title == "Silver Palace", link
        assert item.link is None, link


def test_art_uses_the_cover_allowlist():
    for host in ALLOWED_ART_HOSTS:
        url = f"https://{host}/art.jpg"
        [item] = upcoming.parse({"upcoming": [row(art=url)]})
        assert item.art == url
    for art in (
        "http://cdn2.unrealengine.com/a.jpg",
        "https://evil.example/a.jpg",
        "file:///tmp/a.jpg",
        "https://u:p@cdn2.unrealengine.com/a.jpg",
        "https://cdn2.unrealengine.com:443/a.jpg",
        "https://[broken",
    ):
        [item] = upcoming.parse({"upcoming": [row(art=art)]})
        assert item.title == "Silver Palace"
        assert item.art is None


def test_cache_serves_offline_and_backs_off_after_a_failure():
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return {"upcoming": [row()]}

    first = upcoming.load(URL, fetch=fetch, now=0)
    assert first
    assert upcoming.load(URL, fetch=fetch, now=1) == first
    assert len(calls) == 1

    def offline(*args):
        calls.append("offline")
        raise OSError("offline")

    assert upcoming.load(URL, fetch=offline, now=25000) == first  # past six hours: stale copy
    assert upcoming.load(URL, fetch=offline, now=25001) == first  # within the one-hour backoff
    assert calls.count("offline") == 1
    assert upcoming.load(URL, fetch=lambda *a: {"upcoming": []}, now=30000) == []
    assert upcoming.load(URL, fetch=offline, now=60000) == []


def test_missing_empty_and_disabled_feeds_are_quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(upcoming.hosting, "index_base", lambda: "https://index.example/")
    assert upcoming.feed_url() == "https://index.example/catalog/upcoming.json"
    for url in ("", "http://index.example/a", "file:///tmp/a"):
        assert upcoming.load(url, fetch=lambda *a: calls.append(a)) == []
    assert not calls
    assert upcoming.load(URL, fetch=lambda *a: (_ for _ in ()).throw(FileNotFoundError())) == []
    assert upcoming.load("https://index.example/empty-map.json", fetch=lambda *a: {}) == []
    assert (
        upcoming.load("https://index.example/empty-list.json", fetch=lambda *a: {"upcoming": []})
        == []
    )
    monkeypatch.setattr(upcoming.hosting, "index_base", lambda: "")
    assert upcoming.feed_url() == ""
    assert upcoming.load(fetch=lambda *a: calls.append(a)) == []
    assert not calls


def test_an_oversized_document_is_refused():
    cache = paths.cache_dir() / upcoming.CACHE_NAME
    cache.unlink(missing_ok=True)
    huge = {"upcoming": [row()], "pad": "é" * 40000}
    assert upcoming.load(URL, fetch=lambda *a: huge) == []
    cache.unlink(missing_ok=True)


def test_default_fetch_uses_the_hardened_index_opener(monkeypatch):
    seen = []

    def open_bytes(url, timeout, max_bytes):
        seen.append((url, timeout, max_bytes))
        return b'{"upcoming": []}'

    monkeypatch.setattr(upcoming.indexfetch, "open_bytes", open_bytes)
    assert upcoming.load(URL, now=0) == []
    assert seen == [(URL, 10, upcoming.MAX_BYTES)]


def test_a_self_hosted_index_on_its_own_port_still_gets_the_feed():
    """The feed URL is the user's configured index, so a port is allowed there (not on cards)."""
    url = "https://index.example:8443/catalog/upcoming.json"
    got = upcoming.load(url, fetch=lambda *a: {"upcoming": [row()]}, now=0)
    assert [item.title for item in got] == [row()["title"]]
    for bad in ("https://user@index.example:8443/a.json", "http://index.example:8443/a.json"):
        assert upcoming.load(bad, fetch=lambda *a: {"upcoming": [row()]}, now=0) == []
