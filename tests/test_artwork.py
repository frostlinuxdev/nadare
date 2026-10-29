# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the optional artwork fetcher.

``curl_cffi`` is replaced by an in-memory fake, so no test touches the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import artwork


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
    """A minimal stand-in exposing only what ``artwork`` calls."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _lookup(self, url: str) -> FakeResponse:
        for key, value in self.routes.items():
            if callable(key):
                if key(url):
                    return value
            elif key == url:
                return value
        return FakeResponse(status_code=404)

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._lookup(url)

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._lookup(url)


class ChunkedResponse(FakeResponse):
    """A response that streams chunks, so the image cap is exercised while reading."""

    def __init__(self, chunks: list[bytes], status_code: int = 200, headers=None):
        super().__init__(status_code=status_code, content=b"".join(chunks), headers=headers)
        self._chunks = chunks

    def iter_content(self, chunk_size: int = 65536):
        yield from self._chunks


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRequests:
    requests = FakeRequests({})
    monkeypatch.setattr(artwork, "_curl", lambda: requests)
    monkeypatch.delenv("SGDB_API_KEY", raising=False)
    return requests


# ---------------------------------------------------------------------------
# local resolution
# ---------------------------------------------------------------------------
def test_resolve_returns_first_existing_image(tmp_path: Path) -> None:
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"x")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"x")

    profile = {"cover": str(cover), "thumb": str(thumb)}
    assert artwork.resolve(profile) == cover

    # A missing higher-priority key falls through to the next one.
    assert artwork.resolve({"cover": str(tmp_path / "missing.jpg"), "thumb": str(thumb)}) == thumb
    assert artwork.resolve({"cover": str(tmp_path / "missing.jpg")}) is None


def test_apply_sets_cover() -> None:
    profile: dict = {}
    artwork.apply(profile, "/cache/game-cover.jpg")
    assert profile["cover"] == "/cache/game-cover.jpg"
    assert profile["art_source"] == {"cover": "user"}


# ---------------------------------------------------------------------------
# provenance precedence
# ---------------------------------------------------------------------------
def _file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


def test_official_hero_beats_thirdparty_cover(tmp_path: Path) -> None:
    cover = _file(tmp_path, "thirdparty-cover.jpg")
    hero = _file(tmp_path, "official-hero.jpg")
    profile = {
        "cover": str(cover),
        "hero": str(hero),
        "art_source": {"cover": "thirdparty", "hero": "official"},
    }
    assert artwork.resolve(profile) == hero
    assert artwork.ordered_paths(profile)[0] == str(hero)


def test_user_art_beats_official(tmp_path: Path) -> None:
    hero = _file(tmp_path, "official-hero.jpg")
    custom = _file(tmp_path, "user-bg.jpg")
    profile = {
        "hero": str(hero),
        "custom_background": str(custom),
        "art_source": {"hero": "official", "custom_background": "user"},
    }
    assert artwork.resolve(profile) == custom


def test_legacy_cached_file_is_thirdparty(tmp_path: Path) -> None:
    artwork.cache_dir().mkdir(parents=True, exist_ok=True)
    legacy = artwork.cache_dir() / "game-hero.jpg"
    legacy.write_bytes(b"x")
    profile = {"hero": str(legacy)}
    assert artwork.source_of(profile, "hero", str(legacy)) == "thirdparty"


def _official_file(url: str, slug: str = "game", kind: str = "official-hero") -> Path:
    """Create a file that *looks* like an official download wrote it."""
    cache = artwork.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{slug}-{kind}.jpg"
    path.write_bytes(b"official")
    artwork.src_sidecar(path).write_text(url)
    return path


def test_record_official_overwrites_thirdparty_but_not_user(tmp_path: Path) -> None:
    old = _file(tmp_path, "old.jpg")
    new = _official_file("https://cdn.example/new.jpg")
    thirdparty = {"hero": str(old), "art_source": {"hero": "thirdparty"}}
    assert artwork.record_official(thirdparty, new, "hero") is True
    assert thirdparty["hero"] == str(new)
    assert thirdparty["art_source"]["hero"] == "official"

    user = {"custom_background": str(old), "art_source": {"custom_background": "user"}}
    assert artwork.record_official(user, new, "custom_background") is False
    assert user["custom_background"] == str(old)


def test_record_thirdparty_never_overwrites_official(tmp_path: Path) -> None:
    official = _file(tmp_path, "official.jpg")
    scraped = _file(tmp_path, "scraped.jpg")
    profile = {"hero": str(official), "art_source": {"hero": "official"}}
    assert artwork.record_thirdparty(profile, scraped, "hero") is False
    assert profile["hero"] == str(official)


def test_unknown_source_treated_as_thirdparty(tmp_path: Path) -> None:
    unknown = _file(tmp_path, "whatever.jpg")
    profile = {"cover": str(unknown)}
    assert artwork.source_of(profile, "cover", str(unknown)) == "thirdparty"


def test_record_official_rejects_files_the_fetch_did_not_write(tmp_path: Path) -> None:
    """A stale third-party cache file must never be relabelled as official."""
    artwork.cache_dir().mkdir(parents=True, exist_ok=True)
    stale = artwork.cache_dir() / "game-hero.png"
    stale.write_bytes(b"SGDB")
    profile = {"hero": str(stale), "art_source": {"hero": "thirdparty"}}
    assert artwork.record_official(profile, stale, "hero") is False
    assert profile["art_source"]["hero"] == "thirdparty"

    # Even a file carrying the official name is not trusted without the sidecar.
    bogus = artwork.cache_dir() / "game-official-hero.jpg"
    bogus.write_bytes(b"evil")
    assert artwork.record_official({}, bogus, "hero") is False


def test_fetch_all_does_not_clobber_official(monkeypatch, tmp_path: Path) -> None:
    official = _file(tmp_path, "official.jpg")
    scraped = _file(tmp_path, "scraped.jpg")
    profile = {"hero": str(official), "art_source": {"hero": "official"}}
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: scraped)
    assert artwork.fetch_all(profile, kinds=("hero",)) == {}
    assert profile["hero"] == str(official)


# ---------------------------------------------------------------------------
# source selection
# ---------------------------------------------------------------------------
def test_fetch_prefers_steam_cdn(fake: FakeRequests) -> None:
    base = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/"
    fake.routes[base + "library_600x900.jpg"] = FakeResponse(status_code=404)
    fake.routes[base + "header.jpg"] = FakeResponse(
        content=b"HEADER", headers={"Content-Type": "image/jpeg"}
    )

    path = artwork.fetch({"steam_appid": 730, "title": "Counter-Strike"}, kind="cover")

    assert path is not None
    assert path.name == "counter-strike-cover.jpg"
    assert path.read_bytes() == b"HEADER"
    assert fake.calls[0][1] == base + "library_600x900.jpg"
    assert fake.calls[1][1] == base + "header.jpg"


def test_fetch_uses_steamgriddb_when_key_present(
    fake: FakeRequests, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SGDB_API_KEY", "s3cret")
    fake.routes["https://www.steamgriddb.com/api/v2/search/autocomplete/Nier"] = FakeResponse(
        payload={"data": [{"id": 42, "name": "NieR"}]}
    )
    fake.routes["https://www.steamgriddb.com/api/v2/grids/game/42"] = FakeResponse(
        payload={"data": [{"url": "https://cdn.example/nier.png"}]}
    )
    fake.routes["https://cdn.example/nier.png"] = FakeResponse(
        content=b"PNG", headers={"Content-Type": "image/png"}
    )

    path = artwork.fetch({"title": "Nier"}, kind="cover")

    assert path is not None
    assert path.name == "nier-cover.png"
    assert path.read_bytes() == b"PNG"
    auth = [kw.get("headers", {}).get("Authorization") for _, _, kw in fake.calls]
    assert "Bearer s3cret" in auth


def test_keyless_steamgriddb_public_path_is_removed() -> None:
    """Third-party art scraping is off by default and the keyless endpoint is gone."""
    assert not hasattr(artwork, "SGDB_PUBLIC")
    assert not hasattr(artwork, "_sgdb_public_candidates")


def test_third_party_sources_skipped_by_default(fake: FakeRequests) -> None:
    """No SGDB key and no opt-in: neither SteamGridDB nor AniList is queried."""
    assert (
        artwork.fetch({"steamgriddb_id": 5506287, "anilist": "Arknights"}, kind="cover")
        is None
    )
    assert fake.calls == []


def test_fetch_falls_back_to_anilist(fake: FakeRequests, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADARE_ART_THIRD_PARTY", "1")
    fake.routes["https://graphql.anilist.co"] = FakeResponse(
        payload={
            "data": {
                "Media": {"coverImage": {"extraLarge": "https://img.example/arknights.jpg"}}
            }
        }
    )
    fake.routes["https://img.example/arknights.jpg"] = FakeResponse(
        content=b"ANIME", headers={"Content-Type": "image/jpeg"}
    )

    path = artwork.fetch({"anilist": "Arknights"}, kind="cover")

    assert path is not None
    assert path.name == "arknights-cover.jpg"
    assert path.read_bytes() == b"ANIME"
    post = next(call for call in fake.calls if call[0] == "POST")
    assert post[1] == "https://graphql.anilist.co"
    assert post[2]["json"]["variables"] == {"search": "Arknights"}


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------
def test_second_fetch_uses_cache(fake: FakeRequests) -> None:
    url = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/header.jpg"
    fake.routes["https://cdn.cloudflare.steamstatic.com/steam/apps/730/library_600x900.jpg"] = (
        FakeResponse(status_code=404)
    )
    fake.routes[url] = FakeResponse(content=b"ONCE", headers={"Content-Type": "image/jpeg"})

    profile = {"steam_appid": 730, "title": "CS"}
    first = artwork.fetch(profile, kind="cover")
    calls_after_first = list(fake.calls)
    second = artwork.fetch(profile, kind="cover")

    assert first == second
    assert second is not None and second.read_bytes() == b"ONCE"
    assert fake.calls == calls_after_first  # no second download


def test_fetch_all_records_each_requested_kind(fake: FakeRequests) -> None:
    base = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/"
    fake.routes[base + "library_600x900.jpg"] = FakeResponse(
        content=b"COVER", headers={"Content-Type": "image/jpeg"}
    )
    fake.routes[base + "hero.jpg"] = FakeResponse(
        content=b"HERO", headers={"Content-Type": "image/jpeg"}
    )
    fake.routes[base + "logo.png"] = FakeResponse(
        content=b"LOGO", headers={"Content-Type": "image/png"}
    )

    profile = {"steam_appid": 730, "title": "Test Game"}
    found = artwork.fetch_all(profile)

    assert set(found) == {"cover", "hero", "logo"}
    assert profile["cover"].endswith("test-game-cover.jpg")
    assert profile["hero"].endswith("test-game-hero.jpg")
    assert profile["logo"].endswith("test-game-logo.png")


def test_fetch_returns_none_when_all_sources_fail(fake: FakeRequests) -> None:
    base = "https://cdn.cloudflare.steamstatic.com/steam/apps/1/"
    for name in ("library_600x900.jpg", "header.jpg", "hero.jpg"):
        fake.routes[base + name] = FakeResponse(status_code=404)

    assert artwork.fetch({"steam_appid": 1, "title": "Nothing"}, kind="cover") is None


def test_fetch_swallows_unexpected_errors(fake: FakeRequests, monkeypatch) -> None:
    def boom():
        raise RuntimeError("curl_cffi exploded")

    monkeypatch.setattr(artwork, "_curl", boom)
    assert artwork.fetch({"steam_appid": 1, "title": "X"}, kind="cover") is None


# ---------------------------------------------------------------------------
# official (publisher) images
# ---------------------------------------------------------------------------
def test_host_allowed_requires_https_exact_host_no_userinfo_or_port() -> None:
    hosts = ("cdn.example",)
    assert artwork.host_allowed("https://cdn.example/a.jpg", hosts)
    assert artwork.host_allowed("//cdn.example/a.jpg", hosts)
    assert not artwork.host_allowed("http://cdn.example/a.jpg", hosts)
    assert not artwork.host_allowed("https://evil.example/a.jpg", hosts)
    assert not artwork.host_allowed("https://cdn.example:8443/a.jpg", hosts)
    assert not artwork.host_allowed("https://u:p@cdn.example/a.jpg", hosts)
    assert not artwork.host_allowed("", hosts)


def test_download_official_enforces_allowlist(fake: FakeRequests) -> None:
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        content=b"OFFICIAL", headers={"Content-Type": "image/jpeg"}
    )
    path = artwork.download(
        "https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)
    )
    assert path is not None
    assert path.read_bytes() == b"OFFICIAL"
    assert path.name == "game-hero.jpg"

    assert artwork.download("https://evil.example/a.jpg", "game", "hero", ("cdn.example",)) is None


def test_download_uses_no_browser_impersonation(fake: FakeRequests) -> None:
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        content=b"X", headers={"Content-Type": "image/jpeg"}
    )
    artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",))
    assert fake.calls
    assert "impersonate" not in fake.calls[0][2]


def test_empty_allowlist_never_marks_official(fake: FakeRequests) -> None:
    """M1: without a trust anchor the bytes are cached as third-party art."""
    url = "https://cdn.example/a.jpg"
    fake.routes[url] = FakeResponse(content=b"PUB", headers={"Content-Type": "image/jpeg"})
    path = artwork.download(url, "game", "official-hero", ())
    assert path is not None
    assert path.name == "game-official-hero.jpg"
    assert not artwork.is_official_file(path)
    assert artwork.record_official({"hero": str(path)}, path, "hero") is False


def test_host_allowed_empty_allowlist_rejects_local_hosts() -> None:
    """M1: an empty allowlist must still refuse loopback/private targets."""
    for url in (
        "https://127.0.0.1/a.jpg",
        "https://10.0.0.5/a.jpg",
        "https://192.168.1.1/a.jpg",
        "https://localhost/a.jpg",
        "https://intranet/a.jpg",
    ):
        assert not artwork.host_allowed(url, ())
    assert artwork.host_allowed("https://cdn.example/a.jpg", ())


def test_download_disables_redirects(fake: FakeRequests) -> None:
    """M2: a 3xx must not be followed to an unlisted host."""
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        status_code=302, headers={"Location": "http://127.0.0.1/x"}
    )
    assert (
        artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)) is None
    )
    assert fake.calls[0][2]["allow_redirects"] is False


def test_download_rejects_declared_oversize(fake: FakeRequests) -> None:
    """M3: an oversized Content-Length is refused before the body is read."""
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        content=b"", headers={"Content-Length": str(artwork.MAX_IMAGE_BYTES + 1)}
    )
    assert (
        artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)) is None
    )


def test_download_rejects_oversized_chunked_body(fake: FakeRequests) -> None:
    chunks = [b"x" * 65536] * (artwork.MAX_IMAGE_BYTES // 65536 + 2)
    fake.routes["https://cdn.example/a.jpg"] = ChunkedResponse(chunks)
    assert (
        artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)) is None
    )


def test_store_fsyncs_before_replace(fake: FakeRequests, monkeypatch) -> None:
    """L16: the cached image is durable, not merely renamed into place."""
    url = "https://cdn.example/a.jpg"
    fake.routes[url] = FakeResponse(content=b"IMG", headers={"Content-Type": "image/jpeg"})
    fsyncs: list[int] = []
    real_fsync = artwork.os.fsync

    def fake_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(artwork.os, "fsync", fake_fsync)
    path = artwork.download(url, "game", "hero", ("cdn.example",))
    assert path is not None
    assert fsyncs


# ---------------------------------------------------------------------------
# official cache names are separate from third-party ones
# ---------------------------------------------------------------------------
def test_official_fetch_ignores_stale_thirdparty_file(fake: FakeRequests) -> None:
    """The old SGDB ``{slug}-hero`` must not shadow the official download."""
    artwork.cache_dir().mkdir(parents=True, exist_ok=True)
    stale = artwork.cache_dir() / "arknights-hero.png"
    stale.write_bytes(b"SGDB")

    url = "https://launcher-pkg-ark-en.yo-star.com/img/bg.jpg"
    fake.routes[url] = FakeResponse(content=b"YOSTAR", headers={"Content-Type": "image/jpeg"})
    path = artwork.download(url, "arknights", "official-hero", ("launcher-pkg-ark-en.yo-star.com",))

    assert path is not None
    assert path.name == "arknights-official-hero.jpg"
    assert path.read_bytes() == b"YOSTAR"
    assert stale.read_bytes() == b"SGDB", "the third-party file must be untouched"

    profile = {"hero": str(stale), "art_source": {"hero": "thirdparty"}}
    assert artwork.record_official(profile, path, "hero") is True
    assert profile["hero"] == str(path)
    assert profile["art_source"]["hero"] == "official"
    # The official fetch is what made it official.
    assert artwork.is_official_file(path)


def test_official_download_redownloads_when_url_changes(fake: FakeRequests) -> None:
    first = "https://cdn.example/a.jpg"
    second = "https://cdn.example/b.jpg"
    fake.routes[first] = FakeResponse(content=b"ONE", headers={"Content-Type": "image/jpeg"})
    fake.routes[second] = FakeResponse(content=b"TWO", headers={"Content-Type": "image/png"})

    one = artwork.download(first, "game", "official-hero", ("cdn.example",))
    assert one is not None and one.read_bytes() == b"ONE"
    calls = len(fake.calls)

    # Same URL: a cache hit, no second download.
    again = artwork.download(first, "game", "official-hero", ("cdn.example",))
    assert again == one
    assert len(fake.calls) == calls

    # Changed URL: the stale file must be replaced, not reused.
    changed = artwork.download(second, "game", "official-hero", ("cdn.example",))
    assert changed is not None and changed.read_bytes() == b"TWO"
    assert len(fake.calls) == calls + 1


def test_official_download_redownloads_when_sidecar_missing(fake: FakeRequests) -> None:
    url = "https://cdn.example/a.jpg"
    fake.routes[url] = FakeResponse(content=b"X", headers={"Content-Type": "image/jpeg"})
    one = artwork.download(url, "game", "official-hero", ("cdn.example",))
    assert one is not None
    artwork.src_sidecar(one).unlink()

    two = artwork.download(url, "game", "official-hero", ("cdn.example",))
    assert two == one
    assert len(fake.calls) == 2


def test_official_banner_names_are_namespaced(fake: FakeRequests) -> None:
    url = "https://cdn.example/banner.jpg"
    fake.routes[url] = FakeResponse(content=b"B", headers={"Content-Type": "image/jpeg"})
    path = artwork.download(url, "game", "official-banner-0", ())
    assert path is not None
    assert path.name == "game-official-banner-0.jpg"


# ---------------------------------------------------------------------------
# M1 residual: yostar media keeps its official publisher provenance
# ---------------------------------------------------------------------------
def test_cache_media_keeps_official_provenance(fake: FakeRequests) -> None:
    from nadare.core import yostar

    url = "https://launcher-pkg-ark-en.yo-star.com/img/bg.jpg"
    fake.routes[url] = FakeResponse(content=b"BG", headers={"Content-Type": "image/jpeg"})
    media = yostar.Media(
        game_tag="Arknights_EN", generated_at="t", background=url, banners=()
    )

    result = yostar.cache_media(media, "arknights")

    assert result["hero"] is not None
    path = Path(result["hero"])
    assert artwork.is_official_file(path), "M1: the validated publisher host is a trust anchor"
    profile: dict = {}
    assert artwork.record_official(profile, path, "hero") is True
    assert profile["art_source"]["hero"] == "official"


def test_cache_media_never_fetches_an_off_host_url(fake: FakeRequests) -> None:
    from nadare.core import yostar

    off = "https://evil.example/img/bg.jpg"
    fake.routes[off] = FakeResponse(content=b"EVIL", headers={"Content-Type": "image/jpeg"})
    media = yostar.Media(game_tag="Arknights_EN", generated_at="t", background=off, banners=())

    result = yostar.cache_media(media, "arknights")

    assert result["hero"] is None
    assert fake.calls == []


# ---------------------------------------------------------------------------
# N1: an untrusted slug must never escape the cover cache
# ---------------------------------------------------------------------------
def test_download_rejects_traversal_slug(fake: FakeRequests) -> None:
    """A publisher-controlled slug must never redirect the write outside cache_dir."""
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        content=b"EVIL", headers={"Content-Type": "image/jpeg"}
    )
    hosts = ("cdn.example",)
    for bad in ("../../evil", "..", "../evil", "sub/dir", "a\\b", "evil\x00name", "", None):
        assert artwork.download("https://cdn.example/a.jpg", bad, "thumb", hosts) is None
    # Nothing was written outside the cover cache (old code escaped to the data home).
    data_home = artwork.paths.data_dir().parent
    assert not list(data_home.rglob("evil-thumb.*"))
    assert not list(data_home.rglob("*thumb.jpg"))


def test_store_rejects_unsafe_name(fake: FakeRequests) -> None:
    for bad in ("../x", "..", "a/b", "a\\b", "a\x00b", ""):
        with pytest.raises(ValueError):
            artwork._store("https://cdn.example/a.jpg", bad, "thumb")


def test_safe_slug_still_writes_inside_cache(fake: FakeRequests) -> None:
    fake.routes["https://cdn.example/a.jpg"] = FakeResponse(
        content=b"OK", headers={"Content-Type": "image/jpeg"}
    )
    path = artwork.download("https://cdn.example/a.jpg", "safe-slug", "thumb", ("cdn.example",))
    assert path is not None
    assert path.parent == artwork.cache_dir()
    assert path.read_bytes() == b"OK"
    assert not list(artwork.cache_dir().glob("*.part"))


def test_part_paths_are_unique(tmp_path: Path) -> None:
    dest = tmp_path / "game-hero.jpg"
    first, second = artwork._part_path(dest), artwork._part_path(dest)
    assert first != second
    assert first.parent == dest.parent
    assert first.name.startswith("game-hero.jpg.")
    assert first.name.endswith(".part")


def test_cache_dir_lives_under_data_home() -> None:
    expected = artwork.paths.data_dir() / "covers"
    assert artwork.cache_dir() == expected


class TestSgdbTitleMatch:
    """SteamGridDB search must pick the exact game, never a spinoff."""

    def _fake(self, monkeypatch, results):
        calls = []

        def fake_get_json(url, *, headers=None, timeout=None):
            calls.append(url)
            if "/search/autocomplete/" in url:
                return {"data": results}
            game_id = url.rsplit("/", 1)[-1]
            return {"data": [{"url": f"https://cdn.example/grid-{game_id}.png"}]}

        monkeypatch.setattr(artwork, "_get_json", fake_get_json)
        return calls

    def test_exact_title_wins_over_a_spinoff_listed_first(self, monkeypatch):
        self._fake(
            monkeypatch,
            [
                {"id": 1, "name": "Fate/Grand Order Arcade"},
                {"id": 2, "name": "Fate/Grand Order"},
            ],
        )
        urls = list(artwork._sgdb_candidates({}, "Fate/Grand Order", "key"))
        assert urls == ["https://cdn.example/grid-2.png"]

    def test_match_ignores_case_and_punctuation(self, monkeypatch):
        self._fake(monkeypatch, [{"id": 7, "name": "FATE / GRAND ORDER"}])
        urls = list(artwork._sgdb_candidates({}, "Fate/Grand Order", "key"))
        assert urls == ["https://cdn.example/grid-7.png"]

    def test_no_exact_match_fetches_nothing(self, monkeypatch):
        calls = self._fake(monkeypatch, [{"id": 1, "name": "Fate/Grand Order Arcade"}])
        assert list(artwork._sgdb_candidates({}, "Fate/Grand Order", "key")) == []
        assert len(calls) == 1  # only the search, no grid download

    def test_explicit_sgdb_term_is_matched_too(self, monkeypatch):
        self._fake(
            monkeypatch,
            [{"id": 3, "name": "Arknights: Endfield"}, {"id": 4, "name": "Arknights"}],
        )
        urls = list(artwork._sgdb_candidates({"sgdb": "Arknights"}, "ignored", "key"))
        assert urls == ["https://cdn.example/grid-4.png"]

    def test_explicit_numeric_sgdb_id_skips_the_search(self, monkeypatch):
        calls = self._fake(monkeypatch, [])
        urls = list(artwork._sgdb_candidates({"sgdb_id": 99}, "Anything", "key"))
        assert urls == ["https://cdn.example/grid-99.png"]
        assert not any("/search/" in url for url in calls)

    def test_subtitle_spinoff_alone_is_not_accepted(self, monkeypatch):
        self._fake(monkeypatch, [{"id": 3, "name": "Arknights: Endfield"}])
        assert list(artwork._sgdb_candidates({}, "Arknights", "key")) == []


# ---------------------------------------------------------------------------
# the streamed response is always closed (#44)
# ---------------------------------------------------------------------------
class _ClosableResponse(ChunkedResponse):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.closed = 0

    def close(self):
        self.closed += 1


def test_download_closes_the_response_on_success(fake: FakeRequests) -> None:
    resp = _ClosableResponse([b"IMG"], headers={"Content-Type": "image/jpeg"})
    fake.routes["https://cdn.example/a.jpg"] = resp
    assert artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",))
    assert resp.closed == 1


def test_download_closes_the_response_on_http_error(fake: FakeRequests) -> None:
    resp = _ClosableResponse([b""], status_code=500)
    fake.routes["https://cdn.example/a.jpg"] = resp
    assert artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)) is None
    assert resp.closed == 1


def test_download_closes_the_response_when_the_body_is_too_big(fake: FakeRequests) -> None:
    chunks = [b"x" * 65536] * (artwork.MAX_IMAGE_BYTES // 65536 + 2)
    resp = _ClosableResponse(chunks)
    fake.routes["https://cdn.example/a.jpg"] = resp
    assert artwork.download("https://cdn.example/a.jpg", "game", "hero", ("cdn.example",)) is None
    assert resp.closed == 1


# ------------------------------------------------------------------ catalog games
def _urls(profile: dict, kind: str = "cover") -> list[str]:
    return list(artwork._candidates(profile, None, kind))


def test_a_catalog_game_gets_its_steam_app_art_and_no_site_images() -> None:
    urls = _urls({"backend": "direct-nte"})
    assert urls and all("steamstatic" in u and "/apps/4508340/" in u for u in urls)
    assert not [u for u in urls if "github.io" in u]
    assert not hasattr(artwork, "_site_candidates")


def test_a_game_without_a_steam_id_has_no_catalog_candidates() -> None:
    for kind in ("direct-genshin", "direct-bd2", "launcher-nikke"):
        assert _urls({"backend": kind}) == []
        assert _urls({"backend": kind}, "hero") == []


def test_a_profile_steam_id_wins_over_the_catalog_default() -> None:
    urls = _urls({"backend": "direct-nte", "steam_appid": 111})
    assert "/apps/111/" in urls[0] and not [u for u in urls if "/apps/4508340/" in u]


def test_other_backends_get_no_catalog_candidates() -> None:
    assert _urls({"backend": "arknights-yostar"}) == []
    assert _urls({}) == []


def test_the_art_table_covers_every_catalog_kind_and_only_https_public_hosts() -> None:
    from nadare.core import directkinds as dk

    assert set(dk.ART) == set(dk.CATALOG_KINDS)
    assert all(value is None or isinstance(value, int) for value in dk.ART.values())
    assert not hasattr(dk, "SITE_IMAGES")
    for kind in dk.ART:
        for u in _urls({"backend": kind}, "cover") + _urls({"backend": kind}, "hero"):
            assert artwork.host_allowed(u, ()), u


# ------------------------------------------------------------------ webp recoding
def _png_bytes(width: int = 400, height: int = 300) -> bytes:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(30, 60, 200, 255))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_a_downloaded_png_is_cached_as_a_smaller_webp(fake: FakeRequests) -> None:
    pytest.importorskip("PyQt6.QtGui")
    png = _png_bytes()
    url = "https://cdn.example/hero.png"
    fake.routes[url] = FakeResponse(content=png, headers={"Content-Type": "image/png"})
    path = artwork.download(url, "game", "hero", ("cdn.example",))
    assert path is not None and path.suffix == ".webp"
    assert path.read_bytes()[:4] == b"RIFF" and path.read_bytes()[8:12] == b"WEBP"
    assert path.stat().st_size < len(png)


def test_bytes_qt_cannot_decode_are_cached_unchanged(fake: FakeRequests) -> None:
    url = "https://cdn.example/a.png"
    fake.routes[url] = FakeResponse(content=b"IMG", headers={"Content-Type": "image/png"})
    path = artwork.download(url, "game", "hero", ("cdn.example",))
    assert path is not None and path.suffix == ".png" and path.read_bytes() == b"IMG"


def test_gifs_and_webps_are_not_recoded() -> None:
    assert artwork._recode_webp(b"GIF89a....", "gif") == (b"GIF89a....", "gif")
    assert artwork._recode_webp(b"RIFFxxxxWEBP", "webp") == (b"RIFFxxxxWEBP", "webp")


def test_recoding_is_skipped_when_qt_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_qt(name, *args, **kwargs):
        if name.startswith("PyQt6"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_qt)
    assert artwork._recode_webp(b"\x89PNGdata", "png") == (b"\x89PNGdata", "png")


def test_hoyo_publisher_cover_fills_missing_catalog_art(fake, tmp_path, monkeypatch):
    cache = tmp_path / "covers"
    monkeypatch.setattr(artwork, "cache_dir", lambda: cache)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://hsr.hoyoverse.com/en-us/"
    image = "https://fastcdn.hoyoverse.com/content-v2/hkrpg/share.png"
    fake.routes[page] = FakeResponse(
        content=f'<meta property="og:image" content="{image}">'.encode(),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    profile = {"backend": "direct-hsr", "title": "Honkai: Star Rail"}

    found = artwork.fetch_all(profile, kinds=("cover",))

    assert "cover" in found
    assert profile["art_source"]["cover"] == artwork.SOURCE_OFFICIAL
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == image
    assert any(call[1] == page for call in fake.calls)
    assert any(call[1] == image for call in fake.calls)


def test_hoyo_publisher_cover_offline_falls_back_cleanly(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://genshin.hoyoverse.com/en/home"
    fake.routes[page] = FakeResponse(status_code=503)
    assert artwork.fetch({"backend": "direct-genshin", "title": "Genshin Impact"}) is None


def test_hoyo_publisher_cover_rejects_untrusted_image_host(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://zenless.hoyoverse.com/en-us/"
    fake.routes[page] = FakeResponse(
        content=b'<meta property="og:image" content="https://evil.example/image.png">',
        headers={"Content-Type": "text/html"},
    )
    assert artwork.fetch({"backend": "direct-zzz", "title": "Zenless Zone Zero"}) is None


def test_hoyo_publisher_cover_never_replaces_user_art(fake, tmp_path, monkeypatch):
    cache = tmp_path / "covers"
    monkeypatch.setattr(artwork, "cache_dir", lambda: cache)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://genshin.hoyoverse.com/en/home"
    image = "https://upload-static.hoyoverse.com/hk4e/share.jpg"
    fake.routes[page] = FakeResponse(
        content=f'<meta property="og:image" content="{image}">'.encode(),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    user_cover = _file(tmp_path, "user-cover.png")
    profile = {
        "backend": "direct-genshin",
        "title": "Genshin Impact",
        "cover": str(user_cover),
        "art_source": {"cover": artwork.SOURCE_USER},
    }

    found = artwork.fetch_all(profile, kinds=("cover",))

    assert "cover" not in found
    assert profile["cover"] == str(user_cover)
    assert profile["art_source"]["cover"] == artwork.SOURCE_USER

def test_migrate_cached_cover_writes_webp_and_is_idempotent(tmp_path, monkeypatch):
    source = tmp_path / "game-cover.jpg"
    source.write_bytes(_png_bytes())
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    expected, _ = artwork._recode_webp(source.read_bytes(), "jpg")

    assert artwork.migrate_cached_covers() == 1
    target = tmp_path / "game-cover.webp"
    assert target.read_bytes() == expected
    assert not source.exists()
    assert artwork.migrate_cached_covers() == 0


def test_cached_lookup_runs_the_existing_cover_migration(tmp_path, monkeypatch):
    source = tmp_path / "game-cover.jpg"
    source.write_bytes(_png_bytes())
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    started = []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
            started.append(self)

        def start(self):
            pass

    monkeypatch.setattr(artwork.threading, "Thread", DeferredThread)

    result = artwork.cached("game", "cover")

    assert result == source
    assert source.exists()
    assert len(started) == 1
    started[0].target()
    assert artwork.cached("game", "cover").suffix == ".webp"
    assert not source.exists()


def test_migrate_cached_covers_skips_corrupt_without_raising(tmp_path, monkeypatch):
    source = tmp_path / "bad.png"
    source.write_bytes(b"corrupt")
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(artwork, "_recode_webp", lambda data, ext: (data, ext))
    assert artwork.migrate_cached_covers() == 0
    assert source.read_bytes() == b"corrupt"


def test_interrupted_cover_migration_keeps_original(tmp_path, monkeypatch):
    source = tmp_path / "game-cover.jpg"
    original = _png_bytes()
    source.write_bytes(original)
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    encoded, _ = artwork._recode_webp(original, "jpg")
    monkeypatch.setattr(artwork, "_recode_webp", lambda data, ext: (encoded, "webp"))

    def fail_replace(*_args):
        raise OSError("simulated interruption")

    monkeypatch.setattr(artwork.os, "replace", fail_replace)
    assert artwork.migrate_cached_covers() == 0
    assert source.read_bytes() == original
    assert not (tmp_path / "game-cover.webp").exists()


def test_official_sidecar_precedes_migrated_image_install(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    source = tmp_path / "game-official-hero.png"
    source_sidecar = artwork.src_sidecar(source)
    target = source.with_suffix(".webp")
    target_sidecar = artwork.src_sidecar(target)
    url = "https://publisher.example/hero.png"
    source.write_bytes(_png_bytes())
    source_sidecar.write_text(url)
    real_replace = artwork.os.replace

    def interrupt_image_install(src, dst):
        if Path(dst) == target:
            assert target_sidecar.read_text() == url
            raise OSError("simulated interruption after sidecar install")
        return real_replace(src, dst)

    monkeypatch.setattr(artwork.os, "replace", interrupt_image_install)

    assert artwork.migrate_cached_covers() == 0
    assert source.is_file() and source_sidecar.is_file()
    assert target_sidecar.read_text() == url
    assert not target.exists() or artwork.src_sidecar(target).is_file()


def test_startup_sweep_removes_only_old_inactive_cover_parts(tmp_path, monkeypatch):
    import os
    import time

    root = tmp_path / "covers"
    root.mkdir()
    stale = root / "orphan.part"
    fresh = root / "fresh.part"
    active = root / f"cover.webp.{os.getpid()}.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    active.write_bytes(b"in use")
    old_time = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old_time, old_time))
    os.utime(active, (old_time, old_time))
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"outside")
    os.utime(outside, (old_time, old_time))
    linked = root / "linked.part"
    linked.symlink_to(outside)
    os.utime(linked, (old_time, old_time), follow_symlinks=False)
    monkeypatch.setattr(artwork, "cache_dir", lambda: root)

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(artwork.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(artwork, "migrate_cached_covers", lambda: 0)

    artwork.cached("game", "cover")

    assert not stale.exists()
    assert fresh.exists()
    assert active.exists()
    assert linked.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_migrated_cache_paths_keep_resolving_profile_art(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    hero = tmp_path / "genshin-official-hero.png"
    cover = tmp_path / "genshin-cover.png"
    hero.write_bytes(_png_bytes())
    cover.write_bytes(_png_bytes())
    artwork.src_sidecar(hero).write_text("https://cdn.example/hero.png")
    profile = {
        "hero": str(hero),
        "cover": str(cover),
        "art_source": {"hero": artwork.SOURCE_OFFICIAL, "cover": artwork.SOURCE_THIRDPARTY},
    }

    assert artwork.migrate_cached_covers() == 2

    migrated_hero = tmp_path / "genshin-official-hero.webp"
    migrated_cover = tmp_path / "genshin-cover.webp"
    assert artwork.resolve(profile) == migrated_hero
    assert artwork.resolve_kind(profile, "hero") == migrated_hero
    assert artwork.ordered_paths(profile) == [str(migrated_hero), str(migrated_cover)]


def test_migration_moves_official_sidecar_before_removing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    source = tmp_path / "genshin-official-hero.png"
    url = "https://cdn.example/hero.png"
    source.write_bytes(_png_bytes())
    artwork.src_sidecar(source).write_text(url)

    assert artwork.migrate_cached_covers() == 1

    target = tmp_path / "genshin-official-hero.webp"
    assert artwork.is_official_file(target)
    assert artwork.src_sidecar(target).read_text() == url
    assert artwork._cached_official("genshin-official", "hero", url) == target
    assert not artwork.src_sidecar(source).exists()


def test_migration_sidecar_failure_preserves_original_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    source = tmp_path / "genshin-official-hero.png"
    url = "https://cdn.example/hero.png"
    original = _png_bytes()
    source.write_bytes(original)
    artwork.src_sidecar(source).write_text(url)
    real_replace = artwork.os.replace

    def fail_webp_sidecar(src, dst):
        if Path(dst) == artwork.src_sidecar(source.with_suffix(".webp")):
            raise OSError("simulated sidecar interruption")
        return real_replace(src, dst)

    monkeypatch.setattr(artwork.os, "replace", fail_webp_sidecar)

    assert artwork.migrate_cached_covers() == 0
    assert source.read_bytes() == original
    assert artwork.src_sidecar(source).read_text() == url


def test_migrate_cached_jpeg_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    source = tmp_path / "game-cover.jpeg"
    source.write_bytes(_png_bytes())

    assert artwork.migrate_cached_covers() == 1
    assert (tmp_path / "game-cover.webp").is_file()
    assert not source.exists()


def test_cached_lookup_schedules_one_background_migration(tmp_path, monkeypatch):
    source = tmp_path / "game-cover.jpg"
    source.write_bytes(_png_bytes())
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    started = []

    class FakeThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
            started.append(self)

        def start(self):
            pass

    monkeypatch.setattr(artwork.threading, "Thread", FakeThread)

    artwork.cached("game", "cover")
    artwork.cached("game", "hero")

    assert len(started) == 1
    assert started[0].target is not None


# ------------------------------------------------------------ publisher og:image, more games
_PAGES = {
    "direct-bd2": (
        "https://www.browndust2.com/en-us/",
        "https://www.browndust2.com/img/seo/OGimg_EN_740x340_v5.jpg",
    ),
    "direct-mongil": (
        "https://stardive.netmarble.com/en",
        "https://sgimage.netmarble.com/images/netmarble/monster2/20260305/m.jpg",
    ),
    "launcher-nikke": (
        "https://nikke-en.com/",
        "https://nikke-en.com/pc/ossweb-img/og_image.jpg",
    ),
    "direct-endfield": (
        "https://endfield.gryphline.com/en-us",
        "https://web-static.hg-cdn.com/endfield/official-v4/_next/static/media/share.jpg",
    ),
    "fgo-lepton": ("https://fate-go.us/", "https://fate-go.us/images/fate_card_1800.png"),
}


@pytest.mark.parametrize("kind", sorted(_PAGES))
def test_other_publishers_cover_comes_from_their_official_page(fake, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page, image = _PAGES[kind]
    fake.routes[page] = FakeResponse(
        content=f'<meta property="og:image" content="{image}">'.encode(),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    profile = {"backend": kind, "title": "T"}
    found = artwork.fetch_all(profile, kinds=("cover",))
    assert "cover" in found and profile["art_source"]["cover"] == artwork.SOURCE_OFFICIAL
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == image


def test_a_relative_og_image_resolves_against_the_page(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://www.browndust2.com/en-us/"
    fake.routes[page] = FakeResponse(
        content=b'<meta property="og:image" content="/img/seo/a.jpg">',
        headers={"Content-Type": "text/html"},
    )
    fake.routes["https://www.browndust2.com/img/seo/a.jpg"] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    assert artwork.fetch({"backend": "direct-bd2", "title": "BD2"}) is not None


def test_aether_gazer_uses_its_twitter_image_when_og_image_is_empty(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page = "https://www.aethergazer.com/"
    image = "https://aethergazer.com/twitter_share.png"
    fake.routes[page] = FakeResponse(
        content=(
            b'<meta property="og:image" content="" />'
            b'<meta name="twitter:image" content="' + image.encode() + b'" />'
        ),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    assert artwork.fetch({"backend": "direct-aethergazer", "title": "AG"}) is not None


def test_an_og_image_on_another_host_is_rejected_for_every_publisher(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    for kind, (page, _image) in _PAGES.items():
        fake.routes[page] = FakeResponse(
            content=b'<meta property="og:image" content="https://evil.example/x.png">',
            headers={"Content-Type": "text/html"},
        )
        assert artwork.fetch({"backend": kind, "title": "T"}) is None, kind


def test_an_og_page_that_redirects_is_not_followed(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    fake.routes["https://nikke-en.com/"] = FakeResponse(
        status_code=301, headers={"Location": "http://evil.example/"}
    )
    assert artwork.fetch({"backend": "launcher-nikke", "title": "N"}) is None
    assert not any("evil.example" in call[1] for call in fake.calls)


def test_the_publisher_kinds_are_exactly_the_og_table_and_include_fgo() -> None:
    kinds = artwork.publisher_art_kinds()
    assert kinds == (
        frozenset(artwork._PUBLISHER_OGP)
        | frozenset(artwork._PUBLISHER_FIXED)
        | frozenset(artwork._PUBLISHER_NEWS)
        | frozenset(artwork._PUBLISHER_HOYO)
    )
    assert {"stove-czn", "arknights-yostar", "stellasora-yostar"} <= kinds
    assert {"fgo-lepton", "direct-bd2"} <= kinds


def test_a_fgo_profile_with_its_own_art_never_has_it_replaced(
    fake, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    page, image = _PAGES["fgo-lepton"]
    fake.routes[page] = FakeResponse(
        content=f'<meta property="og:image" content="{image}">'.encode(),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    user_cover = _file(tmp_path, "mine.png")
    profile = {
        "backend": "fgo-lepton",
        "title": "FGO",
        "cover": str(user_cover),
        "art_source": {"cover": artwork.SOURCE_USER},
    }
    artwork.fetch_all(profile, kinds=("cover",))
    assert profile["cover"] == str(user_cover)


# ------------------------------------------------ fixed and news-feed publisher covers
CZN_SHARE = "https://static-pubcomm.onstove.com/live/czn/brand/images/en/ogtag_1200_default.jpg"


def _news_result(*images):
    from nadare.core.news import NewsResult

    return NewsResult(
        items=[
            {"id": str(i), "title": f"t{i}", "image": image, "url": f"https://x/{i}"}
            for i, image in enumerate(images)
        ]
    )


def test_czn_cover_is_its_fixed_official_share_image(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    fake.routes[CZN_SHARE] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    profile = {"backend": "stove-czn", "title": "Chaos Zero Nightmare"}
    found = artwork.fetch_all(profile, kinds=("cover",))
    assert profile["art_source"]["cover"] == artwork.SOURCE_OFFICIAL
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == CZN_SHARE


@pytest.mark.parametrize("kind", ["arknights-yostar", "stellasora-yostar"])
def test_news_games_use_their_newest_news_image_as_the_cover(fake, tmp_path, monkeypatch, kind):
    from nadare.core import backends

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    cls = backends.get(kind)
    host = cls.official_image_hosts[0]
    wanted = f"https://{host}/a.png"
    monkeypatch.setattr(
        cls,
        "news_source",
        staticmethod(
            lambda: _news_result(
                None, "https://evil.example/x.png", wanted, f"https://{host}/b.png"
            )
        ),
    )
    fake.routes[wanted] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    profile = {"backend": kind, "title": "T"}
    found = artwork.fetch_all(profile, kinds=("cover",))
    assert profile["art_source"]["cover"] == artwork.SOURCE_OFFICIAL
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == wanted
    assert not any("evil.example" in call[0] for call in fake.calls)


def test_a_failing_or_imageless_news_feed_leaves_the_placeholder(fake, tmp_path, monkeypatch):
    from nadare.core import backends
    from nadare.core.news import NewsError

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    cls = backends.get("arknights-yostar")

    def boom():
        raise NewsError("503")

    monkeypatch.setattr(cls, "news_source", staticmethod(boom))
    assert artwork.fetch({"backend": "arknights-yostar", "title": "A"}) is None
    monkeypatch.setattr(cls, "news_source", staticmethod(lambda: _news_result(None, None)))
    assert artwork.fetch({"backend": "arknights-yostar", "title": "A"}) is None


def test_the_news_cover_never_replaces_user_art(fake, tmp_path, monkeypatch):
    from nadare.core import backends

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    cls = backends.get("arknights-yostar")
    url = f"https://{cls.official_image_hosts[0]}/a.png"
    monkeypatch.setattr(cls, "news_source", staticmethod(lambda: _news_result(url)))
    fake.routes[url] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    mine = tmp_path / "mine.png"
    mine.write_bytes(_png_bytes())
    profile = {"backend": "arknights-yostar", "title": "A", "cover": str(mine)}
    artwork.set_art_source(profile, "cover", artwork.SOURCE_USER)
    artwork.fetch_all(profile, kinds=("cover",))
    assert profile["cover"] == str(mine)


def test_the_news_cover_falls_through_to_the_next_image_when_one_is_too_big(
    fake, tmp_path, monkeypatch
):
    from nadare.core import backends

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    cls = backends.get("arknights-yostar")
    host = cls.official_image_hosts[0]
    big, small = f"https://{host}/big.png", f"https://{host}/small.png"
    monkeypatch.setattr(cls, "news_source", staticmethod(lambda: _news_result(big, small)))
    fake.routes[big] = FakeResponse(content=b"x" * (artwork.MAX_IMAGE_BYTES + 1))
    fake.routes[small] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    found = artwork.fetch_all({"backend": "arknights-yostar", "title": "A"}, kinds=("cover",))
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == small


# ------------------------------------------------ HoYoverse launcher art (cover + hero)
HOYO_BG = "https://fastcdn.hoyoverse.com/static-resource-v2/2026/09/22/bg.webp"
HOYO_THUMB = "https://launcher-webstatic.hoyoverse.com/launcher-public/2026/03/27/thumb.png"


def _hoyo_art(monkeypatch, result):
    from nadare.backends import hoyo_art

    calls = []

    def fetch_art(game_id, *args, **kwargs):
        calls.append(game_id)
        if isinstance(result, Exception):
            raise result
        return dict(result)

    monkeypatch.setattr(hoyo_art, "fetch_art", fetch_art)
    return calls


@pytest.mark.parametrize(
    ("kind", "game_id"),
    [
        ("direct-hi3", "5TIVvvcwtM"),
        ("direct-hsr", "4ziysqXOQ8"),
        ("direct-genshin", "gopR6Cufr3"),
        ("direct-zzz", "U5hbdsT9W7"),
    ],
)
def test_hoyo_games_take_cover_and_hero_from_the_launcher_feed(
    fake, tmp_path, monkeypatch, kind, game_id
):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    calls = _hoyo_art(monkeypatch, {"hero": HOYO_BG, "cover": HOYO_THUMB})
    # No Steam art for these tests: only the publisher feed may serve the images.
    monkeypatch.setattr(artwork, "_catalog_candidates", lambda *a, **kw: iter(()))
    for url in (HOYO_BG, HOYO_THUMB):
        fake.routes[url] = FakeResponse(
            content=_png_bytes(), headers={"Content-Type": "image/png"}
        )
    profile = {"backend": kind, "title": "T"}
    found = artwork.fetch_all(profile, kinds=("cover", "hero"))
    assert set(found) == {"cover", "hero"}
    assert profile["art_source"] == {
        "cover": artwork.SOURCE_OFFICIAL,
        "hero": artwork.SOURCE_OFFICIAL,
    }
    assert artwork.src_sidecar(found["hero"]).read_text(encoding="utf-8") == HOYO_BG
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == HOYO_THUMB
    assert set(calls) == {game_id}


def test_the_hoyo_feed_is_only_trusted_for_cover_and_hero(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    calls = _hoyo_art(monkeypatch, {"hero": HOYO_BG})
    assert artwork._publisher_covers({"backend": "direct-hi3"}, "logo") == []
    assert calls == []


def test_a_failing_hoyo_feed_leaves_the_placeholder(fake, tmp_path, monkeypatch):
    from nadare.core.news import NewsError

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    monkeypatch.setattr(artwork, "_catalog_candidates", lambda *a, **kw: iter(()))
    _hoyo_art(monkeypatch, NewsError("503"))
    assert artwork.fetch({"backend": "direct-hi3", "title": "H"}, kind="hero") is None
    assert artwork.fetch({"backend": "direct-hi3", "title": "H"}, kind="cover") is None


def test_a_hoyo_image_off_the_official_cdn_is_never_fetched(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    monkeypatch.setattr(artwork, "_catalog_candidates", lambda *a, **kw: iter(()))
    _hoyo_art(monkeypatch, {"hero": "https://evil.example/a.png"})
    assert artwork.fetch({"backend": "direct-hi3", "title": "H"}, kind="hero") is None
    assert not any("evil.example" in call[0] for call in fake.calls)


def test_genshin_falls_back_to_its_page_image_when_the_feed_fails(
    fake, tmp_path, monkeypatch
):
    from nadare.core.news import NewsError

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    monkeypatch.setattr(artwork, "_catalog_candidates", lambda *a, **kw: iter(()))
    _hoyo_art(monkeypatch, NewsError("503"))
    page = "https://genshin.hoyoverse.com/en/home"
    image = "https://upload-static.hoyoverse.com/og.jpg"
    fake.routes[page] = FakeResponse(
        content=f'<meta property="og:image" content="{image}">'.encode(),
        headers={"Content-Type": "text/html"},
    )
    fake.routes[image] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    path = artwork.fetch({"backend": "direct-genshin", "title": "G"}, kind="cover")
    assert path is not None
    assert artwork.src_sidecar(path).read_text(encoding="utf-8") == image


def test_hoyo_art_hosts_cover_both_the_feed_and_the_page_image() -> None:
    hosts = artwork._publisher_image_hosts("direct-genshin")
    assert "launcher-webstatic.hoyoverse.com" in hosts
    assert "upload-static.hoyoverse.com" in hosts
    assert artwork._publisher_image_hosts("direct-hi3") == (
        "fastcdn.hoyoverse.com",
        "launcher-webstatic.hoyoverse.com",
    )


@pytest.mark.parametrize("kind", ["arknights-yostar", "stellasora-yostar"])
def test_a_cover_taken_from_the_news_is_marked_so_its_banner_strip_is_trimmed(
    fake, tmp_path, monkeypatch, kind
):
    from nadare.core import backends
    from nadare.gui import images

    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    cls = backends.get(kind)
    wanted = f"https://{cls.official_image_hosts[0]}/a.png"
    monkeypatch.setattr(cls, "news_source", staticmethod(lambda: _news_result(wanted)))
    fake.routes[wanted] = FakeResponse(content=_png_bytes(), headers={"Content-Type": "image/png"})
    found = artwork.fetch_all({"backend": kind, "title": "T"}, kinds=("cover",))
    assert artwork.is_news_cover(found["cover"])
    assert images.banner_trim_for(found["cover"]) == images.NEWS_BANNER_TRIM


def test_an_already_cached_news_cover_is_marked_on_the_next_fetch(fake, tmp_path, monkeypatch):
    from nadare.gui import images

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "stella-sora-cover.png"
    cached.write_bytes(_png_bytes())
    source = "https://webusstatic.yo-star.com/x/a.png"
    artwork.src_sidecar(cached).write_text(source, encoding="utf-8")
    assert not artwork.is_news_cover(cached)
    got = artwork.fetch({"backend": "stellasora-yostar"}, "Stella Sora", "cover")
    assert got == cached
    assert artwork.is_news_cover(cached)
    assert images.banner_trim_for(cached) == images.NEWS_BANNER_TRIM


def test_a_cover_that_is_not_from_the_news_is_never_trimmed(fake, tmp_path, monkeypatch):
    from nadare.gui import images

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "punishing-gray-raven-cover.png"
    cached.write_bytes(_png_bytes())
    artwork.fetch({"backend": "direct-pgr"}, "Punishing: Gray Raven", "cover")
    assert not artwork.is_news_cover(cached)
    assert images.banner_trim_for(cached) == 0


def test_migration_carries_the_news_marker_to_the_webp(tmp_path, monkeypatch):
    """Library cards read the migrated file without fetching, so the marker must follow it."""
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    source = tmp_path / "stella-sora-cover.png"
    source.write_bytes(_png_bytes())
    artwork.src_sidecar(source).write_text("https://cdn.example/news.png")
    artwork.news_marker(source).touch()

    assert artwork.migrate_cached_covers() == 1

    target = tmp_path / "stella-sora-cover.webp"
    assert artwork.is_news_cover(target)
    assert not artwork.news_marker(source).exists()


def test_an_official_cover_of_a_game_that_has_a_real_cover_is_never_marked(
    fake, tmp_path, monkeypatch
):
    """The cache-hit back-fill must not mark every cover that has a .src sidecar."""
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "genshin-impact-cover.png"
    cached.write_bytes(_png_bytes())
    artwork.src_sidecar(cached).write_text("https://cdn.example/launcher-cover.png")
    artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    assert not artwork.is_news_cover(cached)


def test_a_cover_that_is_already_marked_is_not_touched_again(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "stella-sora-cover.png"
    cached.write_bytes(_png_bytes())
    artwork.src_sidecar(cached).write_text("https://webusstatic.yo-star.com/x/a.png")
    marker = artwork.news_marker(cached)
    marker.touch()
    os.utime(marker, (1_000_000, 1_000_000))
    artwork.fetch({"backend": "stellasora-yostar"}, "Stella Sora", "cover")
    assert marker.stat().st_mtime == 1_000_000


# ---- curated art from the index ---------------------------------------------------------------
INDEX_URL = "https://cdn2.unrealengine.com/egs-genshinimpact-x-s1-2560x1440-abc.jpg"
INDEX_DOC = {
    "schema": 1,
    "games": {
        "direct-genshin": {
            "cover": [{"url": INDEX_URL, "orientation": "wide", "width": 2560, "height": 1440}]
        }
    },
}


def _with_index(monkeypatch, doc=INDEX_DOC):
    from nadare.core import artindex

    artindex.reset()
    artwork._index_failures.clear()
    artindex.set_loader(lambda: doc)
    artindex.lookup("direct-genshin", wait=True)  # loaded, as after a first fetch
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    return artindex


def test_a_cached_cover_is_upgraded_to_the_index_art_when_the_urls_differ(
    fake, tmp_path, monkeypatch
):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    old = covers / "genshin-impact-cover.png"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://old.example/small.png")
    _with_index(monkeypatch)
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )

    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")

    assert got is not None
    assert artwork.src_sidecar(got).read_text(encoding="utf-8") == INDEX_URL
    assert any(INDEX_URL in call[1] for call in fake.calls)


def test_a_cover_that_already_came_from_the_index_url_is_not_fetched_again(
    fake, tmp_path, monkeypatch
):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "genshin-impact-cover.png"
    cached.write_bytes(_png_bytes())
    artwork.src_sidecar(cached).write_text(INDEX_URL)
    _with_index(monkeypatch)
    assert artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover") == cached
    assert not fake.calls


def test_a_failed_index_download_keeps_the_cover_that_is_cached(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    old = covers / "genshin-impact-cover.png"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://old.example/small.png")
    _with_index(monkeypatch)  # no route for INDEX_URL: the download fails
    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    assert got == old
    assert artwork.src_sidecar(old).read_text(encoding="utf-8") == "https://old.example/small.png"


def test_with_nothing_cached_the_index_art_comes_before_the_other_sources(
    fake, tmp_path, monkeypatch
):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    _with_index(monkeypatch)
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    assert got is not None and artwork.src_sidecar(got).read_text(encoding="utf-8") == INDEX_URL
    assert [call[1] for call in fake.calls][0] == INDEX_URL


def test_a_cached_cover_is_returned_at_once_while_the_index_is_still_loading(
    fake, tmp_path, monkeypatch
):
    import threading
    import time

    from nadare.core import artindex

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    cached = covers / "genshin-impact-cover.png"
    cached.write_bytes(_png_bytes())
    gate = threading.Event()
    artindex.reset()
    artindex.set_loader(lambda: gate.wait(10) and INDEX_DOC)
    started = time.monotonic()
    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    took = time.monotonic() - started
    gate.set()
    artindex.wait_for_load(5)
    artindex.reset()
    assert got == cached
    assert took < 1.0, "a cover that is already cached must not wait for the index"


def test_upgrading_a_news_cover_drops_its_news_marker(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_recode_webp", lambda content, ext: (content, "png"))
    old = covers / "stella-sora-cover.png"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://webusstatic.yo-star.com/x/news.png")
    artwork.news_marker(old).touch()
    steam = "https://cdn.cloudflare.steamstatic.com/steam/apps/4813850/library_hero.jpg"
    doc = {
        "schema": 1,
        "games": {
            "stellasora-yostar": {
                "cover": [{"url": steam, "orientation": "wide", "width": 1920, "height": 1080}]
            }
        },
    }
    from nadare.core import artindex

    artindex.reset()
    artwork._index_failures.clear()
    artindex.set_loader(lambda: doc)
    artindex.lookup("stellasora-yostar", wait=True)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    fake.routes[steam] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    got = artwork.fetch({"backend": "stellasora-yostar"}, "Stella Sora", "cover")
    assert got is not None and artwork.src_sidecar(got).read_text(encoding="utf-8") == steam
    assert not artwork.is_news_cover(got)
    assert not artwork.news_marker(old).exists()


def test_a_dead_index_url_is_not_retried_on_every_fetch(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    old = covers / "genshin-impact-cover.png"
    old.write_bytes(_png_bytes())
    _with_index(monkeypatch)  # INDEX_URL has no route: every download fails
    artwork._index_failures.clear()
    for _ in range(3):
        assert artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover") == old
    assert sum(1 for call in fake.calls if call[1] == INDEX_URL) == 1


def test_fetch_all_waits_for_the_index_even_when_a_cover_is_cached(fake, tmp_path, monkeypatch):
    """An explicit fetch (Settings, ``nadare fetch-art``) must apply the index art in one go."""
    import threading

    from nadare.core import artindex

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    old = covers / "genshin-impact-cover.png"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://old.example/small.png")
    artindex.reset()
    artwork._index_failures.clear()
    artindex.set_loader(lambda: INDEX_DOC)  # not loaded yet: only a waiting lookup gets it
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    profile = {"backend": "direct-genshin", "title": "Genshin Impact"}
    found = artwork.fetch_all(profile, kinds=("cover",))
    assert artwork.src_sidecar(found["cover"]).read_text(encoding="utf-8") == INDEX_URL
    assert threading.active_count() >= 1


def test_the_saved_index_copy_upgrades_a_cached_cover_without_waiting(fake, tmp_path, monkeypatch):
    from nadare.core import artindex, indexfetch

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    old = covers / "genshin-impact-cover.png"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://old.example/small.png")
    artindex.reset()
    artwork._index_failures.clear()
    monkeypatch.setattr(artwork, "_recode_webp", lambda content, ext: (content, "png"))
    monkeypatch.setattr(indexfetch, "read_cached", lambda rel, validate=None: INDEX_DOC)
    artindex.set_loader(lambda: INDEX_DOC)
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    artindex.wait_for_load(5)
    assert artwork.src_sidecar(got).read_text(encoding="utf-8") == INDEX_URL


def test_an_older_webp_that_would_shadow_the_new_cover_is_removed(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_recode_webp", lambda content, ext: (content, "png"))
    old = covers / "genshin-impact-cover.webp"
    old.write_bytes(_png_bytes())
    artwork.src_sidecar(old).write_text("https://old.example/small.webp")
    artwork.news_marker(old).touch()
    _with_index(monkeypatch)
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover")
    assert got is not None and got.suffix == ".png"
    assert not old.exists() and not artwork.src_sidecar(old).exists()
    assert not artwork.news_marker(old).exists()
    assert artwork._cached("genshin-impact", "cover") == got


def test_index_art_counts_as_official_and_can_replace_an_official_cover(
    fake, tmp_path, monkeypatch
):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    _with_index(monkeypatch)
    fake.routes[INDEX_URL] = FakeResponse(
        content=_png_bytes(), headers={"Content-Type": "image/png"}
    )
    previous = covers / "previous-cover.png"
    previous.write_bytes(_png_bytes())
    profile = {"backend": "direct-genshin", "title": "Genshin Impact", "cover": str(previous)}
    artwork.set_art_source(profile, "cover", artwork.SOURCE_OFFICIAL)
    found = artwork.fetch_all(profile, kinds=("cover",))
    assert found.get("cover") is not None
    assert artwork.art_source(profile)["cover"] == artwork.SOURCE_OFFICIAL
    assert profile["cover"] == str(found["cover"])
