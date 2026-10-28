# SPDX-License-Identifier: GPL-3.0-or-later
"""The VC redist and WebView2 index lookups use every base and the last good copy."""

from __future__ import annotations

import json

import pytest

from nadare.backends.czn.backend import CznBackend
from nadare.core import indexfetch, vcredist

A = "https://a.example/index/"
B = "https://b.example/index/"
HASH = "ab" * 32
URL = "https://download.visualstudio.microsoft.com/x/vc_redist.x64.exe"
WURL = "https://msedge.sf.dl.delivery.mp.microsoft.com/x/MicrosoftEdgeWebview2Setup.exe"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("NADARE_INDEX_BASE", f"{A} {B}")


class Net:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append(url)
        value = self.routes.get(url, OSError("not routed"))
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode()


def install(monkeypatch, routes):
    net = Net(routes)
    monkeypatch.setattr(indexfetch, "open_bytes", net)
    return net


CASES = [
    ("vcredist/latest.json", vcredist.index_pins, vcredist.index_source, URL),
    (
        "webview2/latest.json",
        CznBackend._webview2_index_pins,
        CznBackend._webview2_index_source,
        WURL,
    ),
]
IDS = ["vcredist", "webview2"]


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_second_base_is_used_when_the_first_is_down(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {B + rel: {"sha256": [HASH], "url": url}})
    assert pins() == {HASH}
    assert source() == url


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_last_good_copy_is_used_when_every_base_is_down(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {A + rel: {"sha256": [HASH], "url": url}})
    assert pins() == {HASH}
    install(monkeypatch, {})
    assert pins() == {HASH}
    assert source() == url


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_nothing_published_and_nothing_cached_is_empty(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {})
    assert pins() == set()
    assert source() == ""


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_a_document_that_is_not_an_object_is_never_cached_or_used(
    monkeypatch, rel, pins, source, url
):
    install(monkeypatch, {A + rel: ["not", "an", "object"], B + rel: ["nope"]})
    assert pins() == set()
    install(monkeypatch, {})
    assert pins() == set()


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_an_off_microsoft_source_is_still_refused(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {A + rel: {"sha256": [HASH], "url": "https://evil.example/x.exe"}})
    assert source() == ""


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_disabled_index_returns_empty(monkeypatch, rel, pins, source, url):
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    net = install(monkeypatch, {})
    assert pins() == set()
    assert source() == ""
    assert net.calls == []


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_an_empty_object_from_the_first_base_does_not_win(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {A + rel: {}, B + rel: {"sha256": [HASH], "url": url}})
    assert pins() == {HASH}
    assert source() == url


@pytest.mark.parametrize("rel,pins,source,url", CASES, ids=IDS)
def test_an_empty_object_is_never_cached(monkeypatch, rel, pins, source, url):
    install(monkeypatch, {A + rel: {}, B + rel: {}})
    assert pins() == set()
    install(monkeypatch, {})
    assert pins() == set()
    assert source() == ""
