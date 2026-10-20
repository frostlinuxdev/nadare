# SPDX-License-Identifier: GPL-3.0-or-later
"""The unsigned czn update index is only ever fetched over https."""

from __future__ import annotations

import io
import json

from nadare.backends.czn import update as czn_update


def _fake_urlopen(calls, body):
    def _open(url, timeout=None):
        calls.append(url)
        return io.BytesIO(body)

    return _open


def _setup(monkeypatch, url):
    calls: list[str] = []
    body = json.dumps({"service": czn_update.SERVICE, "latest": 7}).encode()
    monkeypatch.setattr(czn_update.urllib.request, "urlopen", _fake_urlopen(calls, body))
    monkeypatch.setattr(czn_update, "_SESS", None)
    monkeypatch.setenv("CZN_MANIFEST_INDEX", url)
    return calls


def test_http_index_is_never_fetched(monkeypatch):
    calls = _setup(monkeypatch, "http://idx.example/latest.json")
    assert czn_update.indexed_version() is None
    assert calls == []


def test_https_index_is_used(monkeypatch):
    calls = _setup(monkeypatch, "https://idx.example/latest.json")
    assert czn_update.indexed_version() == 7
    assert calls == ["https://idx.example/latest.json"]
