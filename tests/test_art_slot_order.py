# SPDX-License-Identifier: GPL-3.0-or-later
"""Slot policy: library cover; store wide -> tile -> cover; hero feed -> wide -> hero."""
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork, yostar
from nadare.core.config import Config
from nadare.gui.store import StorePage, _FetchArt
from nadare.gui.workers import BackendWorker


@pytest.mark.parametrize("slot", ["cover", "tile", "hero"])
def test_documented_fallback_order_per_slot(slot, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    calls = []
    profile = {"backend": "direct-wuwa", "title": "Wuthering Waves"}

    def fetch(profile, title=None, kind="cover", **kwargs):
        calls.append("wide" if kwargs.get("art") == "wide" else kind)
        return None

    real_fetch = artwork.fetch
    monkeypatch.setattr(artwork, "fetch", fetch)
    if slot == "cover":
        # The cover slot prefers the index before the existing cover sources.
        monkeypatch.setattr(artwork, "_cached", lambda *a: None)

        def index(*a, **k):
            calls.append("index-cover")
            return None

        def catalog(*a, **k):
            calls.append("cover-sources")
            return iter(())

        monkeypatch.setattr(artwork, "_index_art", index)
        monkeypatch.setattr(artwork, "_catalog_candidates", catalog)
        monkeypatch.setattr(artwork, "_publisher_covers", lambda *a: iter(()))
        monkeypatch.setattr(artwork, "_optional_candidates", lambda *a: iter(()))
        assert real_fetch(profile, profile["title"], "cover") is None
        assert calls == ["index-cover", "cover-sources"]
    elif slot == "tile":
        page = StorePage()
        try:
            _FetchArt("direct-wuwa", profile["title"], page._art_signals).run()
            assert calls == ["wide", "tile", "cover"]
        finally:
            page.deleteLater()
    else:
        # A hero slot only consults the publisher feed for a kind that has one (#1562).
        feed_profile = {"backend": "arknights-yostar", "title": "Arknights"}
        monkeypatch.setattr(yostar, "configured_media", lambda p: "https://example.invalid/media")

        def load(*a, **k):
            calls.append("feed")
            return {}

        monkeypatch.setattr(yostar, "load_media", load)
        monkeypatch.setattr(yostar, "cache_media", lambda *a, **k: {})
        backend = SimpleNamespace(ctx=SimpleNamespace(profile=feed_profile))
        worker = BackendWorker(Config.load(tmp_path / "config.json"), "arknights", "media")
        assert worker._load_media(backend)["hero"] is None
        assert calls == ["feed", "wide", "hero"]
    app.processEvents()
