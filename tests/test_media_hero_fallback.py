# SPDX-License-Identifier: GPL-3.0-or-later
"""A game page always gets the best hero the sources can give it.

Most games have no publisher launcher feed; the index's wide key art (or Steam's library hero)
fills the page. A game *with* a feed keeps it, but falls back to the same wide art when the feed
has no background or cannot be reached, instead of dropping to a plain card.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from nadare.core import artwork, yostar
from nadare.core.config import Config
from nadare.gui import main_window as mw
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendWorker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Ctx:
    profile = {"backend": "arknights-yostar", "title": "Arknights"}


class _Backend:
    game_tag = "arknights"

    def __init__(self) -> None:
        self.ctx = _Ctx()


@pytest.fixture
def wide_art(tmp_path):
    path = tmp_path / "covers" / "arknights-cover-wide.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_a_game_without_a_feed_uses_the_index_wide_art(qapp, tmp_path, monkeypatch, wide_art):
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(yostar, "configured_media", lambda profile: None)
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: wide_art)

    assert BackendWorker(config, "arknights", "media")._load_media(_Backend()) == {
        "kind": "media",
        "hero": str(wide_art),
        "banners": [],
    }


def test_a_feed_without_a_hero_falls_back_to_the_index_wide_art(
    qapp, tmp_path, monkeypatch, wide_art
):
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(yostar, "configured_media", lambda profile: "https://example.invalid/m.json")
    monkeypatch.setattr(yostar, "load_media", lambda source, **kwargs: {"banners": []})
    monkeypatch.setattr(yostar, "cache_media", lambda media, slug, **kwargs: {"banners": []})
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: wide_art)

    result = BackendWorker(config, "arknights", "media")._load_media(_Backend())
    assert result["hero"] == str(wide_art)
    assert result["banners"] == []


def test_a_feed_that_fails_still_gives_the_page_the_wide_art(
    qapp, tmp_path, monkeypatch, wide_art
):
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(yostar, "configured_media", lambda profile: "https://example.invalid/m.json")

    def boom(source, **kwargs):
        raise yostar.MediaError("503")

    monkeypatch.setattr(yostar, "load_media", boom)
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: wide_art)

    result = BackendWorker(config, "arknights", "media")._load_media(_Backend())
    assert result["hero"] == str(wide_art)
    assert result["banners"] == []


def test_without_any_art_the_page_keeps_its_plain_card(qapp, tmp_path, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(yostar, "configured_media", lambda profile: None)
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)

    assert BackendWorker(config, "arknights", "media")._load_media(_Backend()) == {
        "kind": "media",
        "hero": None,
        "banners": [],
    }


class _CatalogBackend:
    """A game with no publisher feed: only the index wide or Steam art can fill the page."""

    def __init__(self, profile: dict) -> None:
        self.ctx = SimpleNamespace(profile=profile)


def test_a_catalog_game_does_not_read_the_yostar_media_feed(qapp, tmp_path, monkeypatch, wide_art):
    """The fallback is for games without a publisher feed; it must not ask Yostar for one."""
    config = Config.load(tmp_path / "config.json")
    asked: list[str] = []
    monkeypatch.setattr(
        yostar, "configured_media", lambda profile: "https://index.nadare.app/yostar/index.json"
    )
    monkeypatch.setattr(yostar, "load_media", lambda *a, **k: asked.append("feed") or {})
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: wide_art)
    profile = {"backend": "direct-wuwa", "title": "Wuthering Waves"}

    result = BackendWorker(config, "wuwa", "media")._load_media(_CatalogBackend(profile))

    assert asked == [], "a catalog game has no Yostar media feed"
    assert result == {"kind": "media", "hero": str(wide_art), "banners": []}


def _window_with_game(tmp_path, monkeypatch, kind: str) -> MainWindow:
    """A real window whose workers never run: only the trigger decisions matter."""
    monkeypatch.setattr(mw.BackendController, "start", lambda *a, **k: False)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "game",
        backend=kind,
        title="Game",
        install_dir=str(tmp_path / "game"),
        prefix=str(tmp_path / "prefix"),
    )
    return MainWindow(config, profile="game")


def test_a_catalog_game_page_starts_the_hero_fetch(tmp_path, qapp, monkeypatch):
    """#1562's wide/Steam hero must be reachable from the real page-open trigger."""
    window = _window_with_game(tmp_path, monkeypatch, "direct-wuwa")
    started: list[tuple[str, str]] = []
    painted: list[str] = []
    try:
        monkeypatch.setattr(
            window, "_start", lambda profile, op, *a, **k: started.append((profile, op)) or True
        )
        monkeypatch.setattr(window.game, "set_media", lambda *a, **k: painted.append("set_media"))
        window.open_game("game")
        # Fresh news: _after_status goes on to media instead of starting the feed first.
        window._snapshots.setdefault("game", {})["news_at"] = time.monotonic()
        window._after_status("game")

        assert ("game", "media") in started, f"no hero fetch started: {started}"
        assert painted == [], "the trigger must not paint the hero by itself"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_catalog_game_page_gets_the_index_wide_hero_through_the_trigger(
    tmp_path, qapp, monkeypatch
):
    """The real page open runs the worker that turns cached wide art into the hero."""
    from nadare.core import artindex

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(artwork, "cache_dir", lambda: covers)
    monkeypatch.setattr(artwork, "_schedule_cover_migration", lambda root: None)
    monkeypatch.setattr(artwork, "_public_resolution", lambda _url: True)
    wide_url = "https://shared.akamai.steamstatic.com/store_item_assets/wuwa/wide.png"
    doc = {
        "schema": 1,
        "games": {
            "direct-wuwa": {
                "wide": [{"url": wide_url, "orientation": "wide", "width": 2560, "height": 1440}]
            }
        },
    }
    artindex.reset()
    artindex.set_loader(lambda: doc)

    class _Response:
        status_code = 200
        content = PNG
        headers = {"Content-Type": "image/png"}

        def close(self):
            pass

    class _Requests:
        def __init__(self):
            self.calls = []

        def get(self, url, **_kwargs):
            self.calls.append(url)
            return _Response()

    transport = _Requests()
    monkeypatch.setattr(artwork, "_curl", lambda: transport)

    window = _window_with_game(tmp_path, monkeypatch, "direct-wuwa")
    heroes: list[str | None] = []
    try:

        def start(profile, op, *a, **k):
            if op == "media":
                backend = _CatalogBackend(window.config.profiles[profile])
                result = BackendWorker(window.config, profile, "media")._load_media(backend)
                heroes.append(result.get("hero"))
            return True

        monkeypatch.setattr(window, "_start", start)
        window.open_game("game")
        window._snapshots.setdefault("game", {})["news_at"] = time.monotonic()
        window._after_status("game")

        assert heroes and heroes[0] is not None, "the page opened without a hero"
        assert "-cover-wide." in Path(heroes[0]).name, heroes[0]
        assert Path(heroes[0]).is_file()
        assert transport.calls == [wide_url]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        artindex.reset()


def test_a_fresh_media_snapshot_is_not_fetched_again_on_the_next_page_open(
    tmp_path, qapp, monkeypatch
):
    """Opening the page again right away must not repeat the art requests."""
    window = _window_with_game(tmp_path, monkeypatch, "direct-wuwa")
    started: list[tuple[str, str]] = []
    try:
        monkeypatch.setattr(
            window, "_start", lambda profile, op, *a, **k: started.append((profile, op)) or True
        )
        window.open_game("game")
        window._snapshots.setdefault("game", {}).update(
            {"news_at": time.monotonic(), "media_at": time.monotonic()}
        )
        window._after_status("game")
        assert ("game", "media") not in started
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("feed", ["missing", "empty", "failed", "hero"])
def test_all_feed_outcomes_share_the_same_hero_fallback(qapp, tmp_path, monkeypatch, feed):
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(yostar, "configured_media", lambda p: None if feed == "missing" else "feed")

    def load(*a, **k):
        if feed == "failed":
            raise yostar.MediaError("offline")
        return {}

    monkeypatch.setattr(yostar, "load_media", load)
    monkeypatch.setattr(yostar, "cache_media", lambda *a, **k: {
        "hero": "publisher.jpg" if feed == "hero" else None, "banners": []
    })
    calls = []

    def fetch(profile, title, kind, **kwargs):
        calls.append((kind, kwargs.get("art")))
        return None if kwargs.get("art") == "wide" else tmp_path / "steam-hero.jpg"

    monkeypatch.setattr(artwork, "fetch", fetch)
    result = BackendWorker(config, "arknights", "media")._load_media(_Backend())
    if feed == "hero":
        assert result["hero"] == "publisher.jpg"
        assert calls == []
    else:
        assert result["hero"] == str(tmp_path / "steam-hero.jpg")
        assert calls == [("cover", "wide"), ("hero", None)]
