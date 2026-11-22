# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the published Kuro launcher tokens and their use by the sources."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core import hosting, kurotokens, paths
from nadare.core.sources import kuro as kurosrc
from nadare.core.sources import wuwa

FIX = Path(__file__).parent / "fixtures" / "catalog"
NEW_TOKEN = "NEWTOKEN12345678"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def payload(wuwa_token: str = "wuwaTOKEN123456", pgr_token: str = "pgrTOKEN123456") -> bytes:
    return json.dumps(
        {"version": 1, "tokens": {"wuwa": f"50004_{wuwa_token}", "pgr": f"50015_{pgr_token}"}}
    ).encode()


class FakeFetch:
    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


def _cache() -> Path:
    """The cache for the source in use now (#1472: one cache per source)."""
    return kurotokens._cache_path(hosting.kuro_tokens_url())


def _set_mtime(path: Path, age: float) -> None:
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


# --------------------------------------------------------------------- parse
def test_parse_returns_only_the_token():
    assert kurotokens.parse(payload(), "wuwa") == "wuwaTOKEN123456"
    assert kurotokens.parse(payload(), "pgr") == "pgrTOKEN123456"


@pytest.mark.parametrize(
    "body",
    [
        b'{"version": 1, "tokens": {"wuwa": "50015_wrongPREFIX12345"}}',
        b'{"version": 1, "tokens": {"wuwa": "50004_bad-token!"}}',
        b'{"version": 1, "tokens": {"wuwa": "50004_short"}}',
        b'{"version": 1, "tokens": {"wuwa": "50004_' + b"a" * 65 + b'"}}',
        b'{"version": 1, "tokens": {"pgr": "50015_pgrTOKEN123456"}}',
        b'{"version": 1, "tokens": []}',
        b"[]",
        b"not json",
        b"\xff\xfe\x00garbage",
    ],
)
def test_parse_rejects_bad_payloads(body):
    assert kurotokens.parse(body, "wuwa") is None


# ----------------------------------------------------------------- published
def test_published_uses_a_fresh_cache_without_network(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    cache = _cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload())

    def opener(url):
        raise AssertionError("network must not be called for a fresh cache")

    assert kurotokens.published("wuwa", opener=opener) == "wuwaTOKEN123456"


def test_published_refetches_a_stale_cache_and_rewrites_it(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    cache = _cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload("oldTOKEN1234567", "oldPGRTOKEN12345"))
    _set_mtime(cache, 25 * 60 * 60)
    fresh = payload(NEW_TOKEN, "newPGRTOKEN1234")

    assert kurotokens.published("wuwa", opener=lambda url: fresh) == NEW_TOKEN
    assert cache.read_bytes() == fresh


def test_published_falls_back_to_a_stale_cache_when_the_fetch_fails(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    cache = _cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload("oldTOKEN1234567"))
    _set_mtime(cache, 25 * 60 * 60)

    def opener(url):
        raise OSError("offline")

    assert kurotokens.published("wuwa", opener=opener) == "oldTOKEN1234567"


def test_published_returns_none_without_a_cache_or_network(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")

    def opener(url):
        raise OSError("offline")

    assert kurotokens.published("wuwa", opener=opener) is None


def test_published_does_nothing_when_the_index_is_disabled(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "")

    calls: list[str] = []

    assert hosting.kuro_tokens_url() == ""
    assert kurotokens.published("wuwa", opener=lambda url: calls.append(url) or b"") is None
    assert calls == []


def test_published_does_not_cache_an_unparsable_payload(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    cache = _cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    old = payload("oldTOKEN1234567")
    cache.write_bytes(old)
    _set_mtime(cache, 25 * 60 * 60)

    assert kurotokens.published("wuwa", opener=lambda url: b"not json") == "oldTOKEN1234567"
    assert cache.read_bytes() == old


# ------------------------------------------------------------------- sources
def _wuwa_routes(new_url: str) -> tuple[dict[str, bytes], str, dict[str, object]]:
    index = json.loads(fx("wuwa_index.json"))
    cfg = index["default"]["config"]
    root = "https://hw-pcdownload-aws.aki-game.net/"
    return {
        new_url: fx("wuwa_index.json"),
        wuwa.INDEX_URL: fx("wuwa_index.json"),
        root + cfg["indexFile"]: fx("wuwa_indexFile.json"),
    }, root, cfg


def test_published_token_is_tried_before_the_builtin(monkeypatch):
    new_url = (
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
        f"50004_{NEW_TOKEN}/index.json"
    )
    routes, _root, cfg = _wuwa_routes(new_url)
    monkeypatch.setattr(kurotokens, "published", lambda game, **kw: NEW_TOKEN)

    calls: list[str] = []

    def fake_fetch(url, headers=None):
        calls.append(url)
        if url == new_url:
            raise dd.DirectError("HTTP 403 for " + url)
        return routes[url]

    monkeypatch.setattr(kurosrc, "default_fetch", fake_fetch)
    plan = wuwa.resolve_legacy()
    assert calls[:2] == [new_url, wuwa.INDEX_URL]
    assert plan.version == cfg["version"]


def test_an_explicit_token_is_the_only_candidate():
    token = "EXPLICITTOKEN123456"
    calls: list[str] = []

    def fake_fetch(url, headers=None):
        calls.append(url)
        raise dd.DirectError("HTTP 403 for " + url)

    with pytest.raises(dd.DirectError):
        wuwa.resolve_legacy(fake_fetch, token=token)
    assert calls == [
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
        f"50004_{token}/index.json"
    ]


def test_an_injected_fetch_never_reads_the_published_tokens(monkeypatch):
    def published(game, **kw):
        raise AssertionError("tests with an injected fetch must stay offline")

    monkeypatch.setattr(kurotokens, "published", published)
    routes, _root, _cfg = _wuwa_routes(wuwa.INDEX_URL)
    assert wuwa.resolve_legacy(FakeFetch(routes)).game_tag == "wuwa"


def test_a_rotation_inside_the_cache_lifetime_is_picked_up_by_one_forced_refresh(monkeypatch):
    old, new = "ROTATEDOLD123456", "ROTATEDNEW123456"
    url = lambda t: (  # noqa: E731
        f"https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/50004_{t}/index.json"
    )
    routes, _root, cfg = _wuwa_routes(url(new))
    seen: list[bool] = []

    def published(game, *, refresh=False, **kw):
        seen.append(refresh)
        return new if refresh else old  # the cache still holds the old token

    monkeypatch.setattr(kurotokens, "published", published)
    calls: list[str] = []

    def fake_fetch(u, headers=None):
        calls.append(u)
        if u == url(new):
            return routes[u]
        if u in routes and u not in (wuwa.INDEX_URL, url(new)):
            return routes[u]
        raise dd.DirectError("HTTP 403 for " + u)

    monkeypatch.setattr(kurosrc, "default_fetch", fake_fetch)
    plan = wuwa.resolve_legacy()
    assert plan.version == cfg["version"]
    assert seen == [False, True]
    assert calls[:3] == [url(old), wuwa.INDEX_URL, url(new)]


def test_no_refresh_is_attempted_for_an_explicit_token(monkeypatch):
    def published(game, **kw):
        raise AssertionError("an explicit token must not consult the index")

    monkeypatch.setattr(kurotokens, "published", published)

    def fake_fetch(u, headers=None):
        raise dd.DirectError("HTTP 403 for " + u)

    with pytest.raises(dd.DirectError):
        wuwa.resolve_legacy(fake_fetch, token="EXPLICITTOKEN123456")


# ----------------------------------------------------------- #1472: bound to its source
def test_a_disabled_index_never_serves_a_cached_token(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    cache = _cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload())  # fresh, from when the index was on
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    calls: list[str] = []  # published() swallows fetch errors: record, do not raise

    assert kurotokens.published("wuwa", opener=lambda url: calls.append(url) or b"") is None
    assert calls == [], "the index is disabled; nothing may be fetched"


def test_a_cache_from_another_source_is_not_used(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://source-a.invalid/")
    fetch_a = FakeFetch({hosting.kuro_tokens_url(): payload("aTOKEN123456789")})
    assert kurotokens.published("wuwa", opener=fetch_a) == "aTOKEN123456789"
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://source-b.invalid/")
    fetch_b = FakeFetch({hosting.kuro_tokens_url(): payload("bTOKEN123456789")})
    assert kurotokens.published("wuwa", opener=fetch_b) == "bTOKEN123456789"
    assert fetch_b.calls == [hosting.kuro_tokens_url()]


def test_an_offline_fallback_uses_only_its_own_sources_cache(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://source-a.invalid/")
    kurotokens.published("wuwa", opener=lambda url: payload("aTOKEN123456789"))
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://source-b.invalid/")

    def offline(url):
        raise OSError("offline")

    assert kurotokens.published("wuwa", opener=offline) is None


def test_the_old_shared_cache_file_is_not_trusted(monkeypatch):
    """0.1.0b24 kept one cache for every source; it cannot say where it came from."""
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.invalid/")
    legacy = paths.cache_dir() / "kuro-tokens.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(payload("legacyTOKEN1234"))

    def offline(url):
        raise OSError("offline")

    assert kurotokens.published("wuwa", opener=offline) is None
