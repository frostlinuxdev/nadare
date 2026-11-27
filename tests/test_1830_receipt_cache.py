# SPDX-License-Identifier: GPL-3.0-or-later
"""#1830: the update receipt counts what came over the network, not files the predownload had.

A predownloaded file taken from the chunk cache is still "done" for the progress bar, but the
receipt ("downloaded N GiB", the far-more-than-the-publisher warning) must not count it again.
``sync``'s summary carries ``fetched_bytes``; the backend's receipt uses it.
"""

from __future__ import annotations

import hashlib

import pytest
from _httpserver import Server
from test_1252_receipt import GB, _backend

from nadare.core import directdl as dd


@pytest.fixture
def serve():
    made = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _rf(server, path, data):
    return dd.RemoteFile(path, f"{server.url}/{path}", len(data), md5=hashlib.md5(data).hexdigest())


def test_sync_reports_only_the_bytes_it_fetched(serve, tmp_path):
    fetched, cached = b"f" * 300, b"c" * 7000
    s = serve({"/fetched.pak": fetched, "/cached.pak": cached})
    plan = dd.Plan("t", "2", files=(_rf(s, "fetched.pak", fetched), _rf(s, "cached.pak", cached)))
    cache = tmp_path / "cache"
    hit = dd.cached_file(cache, plan.files[1])
    hit.parent.mkdir(parents=True, exist_ok=True)
    hit.write_bytes(cached)
    summary = dd.sync(plan, tmp_path / "game", workers=1, chunk_cache=cache)
    assert (tmp_path / "game/cached.pak").read_bytes() == cached
    assert summary.fetched_bytes == len(fetched)


def test_sync_without_a_cache_reports_every_fetched_byte(serve, tmp_path):
    data = b"x" * 1234
    s = serve({"/a.pak": data})
    summary = dd.sync(dd.Plan("t", "1", files=(_rf(s, "a.pak", data),)), tmp_path, workers=1)
    assert summary.fetched_bytes == len(data)


def test_the_receipt_uses_the_fetched_bytes_not_the_progress(tmp_path, monkeypatch):
    # Progress says 88 GB (cache hits included); only 1 GB came over the network.
    backend, events = _backend(tmp_path, monkeypatch, downloaded=88 * GB, publisher=13 * GB)
    fake = dd.sync

    def sync(*args, **kwargs):
        return dd.SyncSummary(**fake(*args, **kwargs), fetched_bytes=GB)

    monkeypatch.setattr(dd, "sync", sync)
    backend._sync_direct()
    assert not any("far more" in e.message for e in events)
    receipt = [e.message for e in events if e.kind == "log" and "update 3.7.2" in e.message]
    assert receipt and "downloaded 954 MiB" in receipt[0], receipt
