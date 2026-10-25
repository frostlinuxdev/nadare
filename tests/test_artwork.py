# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the optional artwork fetcher.

``curl_cffi`` is replaced by an in-memory fake, so no test touches the network.
"""

from __future__ import annotations

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


def test_a_catalog_game_gets_its_steam_app_and_the_site_images() -> None:
    urls = _urls({"backend": "direct-nte"})
    steam = [u for u in urls if "steamstatic" in u]
    site = [u for u in urls if "frostlinuxdev.github.io" in u]
    assert steam and all("/apps/4508340/" in u for u in steam)
    assert site == [
        "https://frostlinuxdev.github.io/images/neverness-to-everness/cover.webp",
        "https://frostlinuxdev.github.io/images/neverness-to-everness/thumb.webp",
        "https://frostlinuxdev.github.io/images/neverness-to-everness/hero.webp",
    ]
    assert urls.index(steam[0]) < urls.index(site[0])  # publisher store art first


def test_hero_art_prefers_the_hero_image_and_falls_back_to_the_thumb() -> None:
    site = [u for u in _urls({"backend": "direct-endfield"}, "hero") if "github.io" in u]
    assert [u.rsplit("/", 1)[1] for u in site] == ["hero.webp", "thumb.webp"]


def test_a_game_without_a_steam_id_still_gets_site_images() -> None:
    urls = _urls({"backend": "direct-genshin"})
    assert not [u for u in urls if "steamstatic" in u]
    assert [u for u in urls if "github.io" in u]


def test_a_profile_steam_id_wins_over_the_catalog_default() -> None:
    urls = _urls({"backend": "direct-nte", "steam_appid": 111})
    assert "/apps/111/" in urls[0] and not [u for u in urls if "/apps/4508340/" in u]


def test_other_backends_get_no_catalog_candidates() -> None:
    assert _urls({"backend": "arknights-yostar"}) == []
    assert _urls({}) == []


def test_the_art_table_covers_every_catalog_kind_and_only_https_public_hosts() -> None:
    from nadare.core import directkinds as dk

    assert set(dk.ART) == set(dk.CATALOG_KINDS)
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
