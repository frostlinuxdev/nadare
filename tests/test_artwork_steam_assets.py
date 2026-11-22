# SPDX-License-Identifier: GPL-3.0-or-later
"""Steam's own library assets, and the index's wide art, in the artwork cache."""

from __future__ import annotations

import base64

import pytest

from nadare.core import artindex, artwork


class _Response:
    def __init__(self, content: bytes = b"", headers=None):
        self.status_code = 200
        self.content = content
        self.headers = headers or {}

    def json(self):
        return {}


class _Requests:
    """Only the two verbs ``artwork`` calls, routed by exact URL."""

    def __init__(self):
        self.routes: dict[str, _Response] = {}
        self.calls: list[tuple[str, str]] = []

    def _lookup(self, url: str) -> _Response:
        return self.routes.get(url, _Response(content=b"", headers={}))

    def get(self, url: str, **_kwargs):
        self.calls.append(("GET", url))
        return self._lookup(url)

    def post(self, url: str, **_kwargs):
        self.calls.append(("POST", url))
        return self._lookup(url)


@pytest.fixture
def fake(monkeypatch):
    requests = _Requests()
    monkeypatch.setattr(artwork, "_curl", lambda: requests)
    return requests


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
APP = "3513350"
CAPSULE = (
    f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{APP}/"
    "aaa/library_capsule_2x.jpg?t=1794439536"
)
HERO = (
    f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{APP}/"
    "bbb/library_hero_2x.jpg?t=1794439536"
)
PORTRAIT = "https://cdn2.unrealengine.com/portrait.jpg"


@pytest.fixture(autouse=True)
def fresh():
    artwork._STEAM_ASSETS.clear()
    artindex.reset()
    yield
    artwork._STEAM_ASSETS.clear()
    artindex.reset()


def test_steam_library_assets_names_the_current_hashed_files(monkeypatch):
    payload = {
        "response": {
            "store_items": [
                {
                    "assets": {
                        "asset_url_format": f"steam/apps/{APP}/${{FILENAME}}?t=1794439536",
                        "library_capsule_2x": "aaa/library_capsule_2x.jpg",
                        "library_hero_2x": "bbb/library_hero_2x.jpg",
                    }
                }
            ]
        }
    }
    calls = []

    def get_json(url):
        calls.append(url)
        return payload

    monkeypatch.setattr(artwork, "_get_json", get_json)
    assets = artwork.steam_library_assets(APP)
    assert assets["library_capsule_2x"] == CAPSULE
    assert assets["library_hero_2x"] == HERO
    # A second lookup inside the window does not ask Steam again.
    artwork.steam_library_assets(APP)
    assert len(calls) == 1


def test_steam_library_assets_never_raises_and_remembers_a_failure(monkeypatch):
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(artwork, "_get_json", boom)
    assert artwork.steam_library_assets(APP) == {}
    assert artwork.steam_library_assets(APP) == {}
    assert len(calls) == 1


def test_a_failed_answer_is_remembered_for_minutes_not_hours(monkeypatch):
    """A flaky request must not cost a game its Steam art for the rest of the session."""
    import time

    payload = {
        "response": {
            "store_items": [
                {"assets": {"asset_url_format": f"steam/apps/{APP}/${{FILENAME}}",
                            "library_capsule_2x": "aaa/library_capsule_2x.jpg"}}
            ]
        }
    }
    calls = []

    def get_json(url):
        calls.append(url)
        return payload

    monkeypatch.setattr(artwork, "_get_json", get_json)
    stale = time.monotonic() - artwork._STEAM_ASSETS_FAILED_TTL - 1
    artwork._STEAM_ASSETS[APP] = (stale, {}, artwork._STEAM_ASSETS_FAILED_TTL)
    assert artwork.steam_library_assets(APP)["library_capsule_2x"].startswith(
        "https://shared.akamai.steamstatic.com/"
    )
    assert calls, "an expired failure is asked again"

    # A good table lives long enough that the same age still answers without a request.
    calls.clear()
    artwork._STEAM_ASSETS[APP] = (
        stale,
        {"library_capsule_2x": CAPSULE},
        artwork._STEAM_ASSETS_TTL,
    )
    assert artwork.steam_library_assets(APP)["library_capsule_2x"] == CAPSULE
    assert calls == []


def test_the_cover_candidates_try_the_portrait_library_capsule_first(monkeypatch):
    monkeypatch.setattr(
        artwork,
        "steam_library_assets",
        lambda appid: {
            "library_capsule_2x": CAPSULE,
            "library_hero_2x": HERO,
        },
    )
    monkeypatch.setattr(artwork, "_steam_store_image", lambda appid: None)
    cover = list(artwork._steam_candidates({"backend": "direct-wuwa"}, "cover"))
    assert cover[0] == CAPSULE
    hero = list(artwork._steam_candidates({"backend": "direct-wuwa"}, "hero"))
    assert hero[0] == HERO


def test_fetch_wide_uses_the_index_wide_list_and_its_own_cache_file(fake, tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    doc = {
        "schema": 1,
        "games": {
            "direct-genshin": {
                "cover": [
                    {
                        "url": PORTRAIT,
                        "orientation": "portrait",
                        "width": 1200,
                        "height": 1600,
                    }
                ],
                "wide": [
                    {
                        "url": "https://cdn2.unrealengine.com/wide.jpg",
                        "orientation": "wide",
                        "width": 2560,
                        "height": 1440,
                    }
                ],
            }
        },
    }
    artindex.set_loader(lambda: doc)
    artindex.lookup("direct-genshin", art="wide", wait=True)  # preloaded, as after a fetch
    fake.routes["https://cdn2.unrealengine.com/wide.jpg"] = _Response(
        content=PNG, headers={"Content-Type": "image/png"}
    )

    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover", art="wide")

    assert got is not None
    assert got.name.startswith("genshin-impact-cover-wide.")
    assert not (covers / "genshin-impact-cover.png").exists()
    # The same URL is not downloaded twice.
    calls = len(fake.calls)
    assert artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover", art="wide")
    assert len(fake.calls) == calls


def test_a_wide_fetch_without_index_art_does_not_fall_back_to_a_cover(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path / "covers")
    artindex.set_loader(lambda: {"schema": 1, "games": {}})
    assert (
        artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover", art="wide") is None
    )
    assert not fake.calls


def test_a_cached_wide_file_is_used_when_the_index_is_unreachable(fake, tmp_path, monkeypatch):
    """A wide file already on disk must never wait for the network (sim bug 2)."""
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_schedule_cover_migration", lambda root: None)
    wide = covers / "genshin-impact-cover-wide.webp"
    wide.write_bytes(PNG)
    artwork.src_sidecar(wide).write_text("https://cdn2.unrealengine.com/wide.jpg")

    def offline():
        raise OSError("index unreachable")

    artindex.set_loader(offline)
    monkeypatch.setattr(artindex, "_read_saved_doc", lambda: None)

    got = artwork.fetch({"backend": "direct-genshin"}, "Genshin Impact", "cover", art="wide")

    assert got == wide
    assert fake.calls == [], f"the cached wide art must not need the network: {fake.calls}"
    artindex.wait_for_load(5)


def test_a_steam_header_image_on_a_foreign_host_is_never_fetched(fake, tmp_path, monkeypatch):
    """A URL inside a Steam API answer is only used when Steam hosts it (sim bug 4)."""
    covers = tmp_path / "covers"
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    foreign = "https://evil.example/steam-header.jpg"
    fake.routes[foreign] = _Response(content=PNG, headers={"Content-Type": "image/png"})
    artindex.set_loader(lambda: {"schema": 1, "games": {}})
    monkeypatch.setattr(
        artwork,
        "_get_json",
        lambda url: {APP: {"success": True, "data": {"header_image": foreign}}},
    )

    got = artwork.fetch({"backend": "direct-wuwa"}, "Wuthering Waves", "cover")

    assert got is None
    assert not any(url == foreign for _verb, url in fake.calls), (
        f"the foreign host was requested: {[u for _v, u in fake.calls if 'evil' in u]}"
    )
    assert artwork.cached("wuthering-waves", "cover") is None


@pytest.mark.parametrize(
    ("kind", "preferred"),
    [("cover", "library_capsule_2x"), ("hero", "library_hero_2x"), ("tile", "main_capsule_2x")],
)
def test_each_steam_slot_uses_its_own_asset_before_legacy_urls(monkeypatch, kind, preferred):
    assets = {name: f"https://shared.akamai.steamstatic.com/{name}.jpg" for name in (
        "library_capsule_2x", "library_hero_2x", "main_capsule_2x"
    )}
    monkeypatch.setattr(artwork, "steam_library_assets", lambda appid: assets)
    candidates = artwork._steam_candidates({"backend": "direct-wuwa"}, kind)
    assert next(candidates) == assets[preferred]


def test_successful_assets_expire_after_six_hours_and_request_assets(monkeypatch):
    import json
    from urllib.parse import parse_qs, urlparse

    now = [100.0]
    calls = []
    monkeypatch.setattr(artwork.time, "monotonic", lambda: now[0])

    def get_json(url):
        calls.append(url)
        payload = json.loads(parse_qs(urlparse(url).query)["input_json"][0])
        assert payload["ids"] == [{"appid": int(APP)}]
        assert payload["data_request"] == {"include_assets": True}
        return {"response": {"store_items": [{"assets": {
            "asset_url_format": f"steam/apps/{APP}/${{FILENAME}}",
            "library_capsule_2x": "capsule.jpg",
        }}]}}

    monkeypatch.setattr(artwork, "_get_json", get_json)
    assert artwork.steam_library_assets(APP)
    now[0] += 6 * 3600 - 1
    assert artwork.steam_library_assets(APP)
    assert len(calls) == 1
    now[0] += 2
    assert artwork.steam_library_assets(APP)
    assert len(calls) == 2


def test_a_real_failure_is_retried_after_minutes(monkeypatch):
    """The failure the function itself records expires on the short TTL, not the six-hour one."""
    now = [1000.0]
    monkeypatch.setattr(artwork.time, "monotonic", lambda: now[0])
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(artwork, "_get_json", boom)
    assert artwork.steam_library_assets(APP) == {}
    assert len(calls) == 1
    now[0] += artwork._STEAM_ASSETS_FAILED_TTL + 1
    assert artwork.steam_library_assets(APP) == {}
    assert len(calls) == 2, "a failure is asked again after minutes"
