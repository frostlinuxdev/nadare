# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare's own hosting URLs and the defaults that use them."""

from __future__ import annotations

from nadare.backends.czn import update as czn_update
from nadare.core import hosting, yostar

BASE = "https://idx.example/nadare-index/raw/"


def test_defaults_point_at_the_index_repo(monkeypatch):
    monkeypatch.delenv("NADARE_INDEX_BASE")
    monkeypatch.delenv("NADARE_RELEASES_API")
    assert hosting.index_base() == hosting.DEFAULT_INDEX_BASE
    assert hosting.index_base().endswith("/")
    assert hosting.yostar_index_url() == hosting.DEFAULT_INDEX_BASE + "yostar/index.json"
    assert hosting.czn_index_url() == hosting.DEFAULT_INDEX_BASE + "czn/latest.json"
    assert hosting.releases_api() == hosting.DEFAULT_RELEASES_API
    assert hosting.releases_api().startswith("https://")


def test_env_override_and_disable(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/x")
    assert hosting.yostar_index_url() == "https://idx.example/x/yostar/index.json"
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert hosting.yostar_index_url() == ""
    assert hosting.czn_index_url() == ""


class TestYostarDefaults:
    def test_manifest_falls_back_to_the_published_index(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert yostar.configured_manifest({}, {}) == BASE + "yostar/index.json"

    def test_explicit_sources_still_win(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert yostar.configured_manifest({"manifest_file": "/m.json"}, {}) == "/m.json"
        assert (
            yostar.configured_manifest({}, {"YOSTAR_MANIFEST_INDEX": "https://e/i.json"})
            == "https://e/i.json"
        )

    def test_no_default_when_disabled(self):
        assert yostar.configured_manifest({}, {}) == ""
        assert yostar.configured_media({}, {}) == ""

    def test_media_falls_back_to_the_index(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert yostar.configured_media({}, {}) == BASE + "yostar/index.json"

    def test_media_follows_an_explicit_index(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        prof = {"manifest_index": "https://p/index.json"}
        assert yostar.configured_media(prof, {}) == "https://p/index.json"
        env = {"YOSTAR_MANIFEST_INDEX": "https://e/index.json"}
        assert yostar.configured_media({}, env) == "https://e/index.json"

    def test_media_file_wins_and_manifest_file_is_not_media(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert yostar.configured_media({"media_file": "/me.json"}, {}) == "/me.json"
        # A single manifest file carries no media: use the published index.
        prof = {"manifest_file": "/m.json"}
        assert yostar.configured_media(prof, {}) == BASE + "yostar/index.json"


class TestCznIndex:
    def test_default_comes_from_hosting(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert czn_update.index_url() == BASE + "czn/latest.json"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        monkeypatch.setenv("CZN_MANIFEST_INDEX", "https://c/latest.json")
        assert czn_update.index_url() == "https://c/latest.json"
        monkeypatch.setenv("CZN_MANIFEST_INDEX", "")
        assert czn_update.index_url() == ""

    def test_indexed_version_skips_when_disabled(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not fetch")

        monkeypatch.setattr(czn_update.urllib.request, "urlopen", boom)
        monkeypatch.setattr(czn_update, "_SESS", None)
        assert czn_update.indexed_version() is None


class TestVcredistIndexUrl:
    def test_default_comes_from_hosting(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
        assert hosting.vcredist_index_url() == BASE + "vcredist/latest.json"

    def test_disabled_index_yields_empty(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", "")
        assert hosting.vcredist_index_url() == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/x")
        assert hosting.vcredist_index_url() == "https://idx.example/x/vcredist/latest.json"
