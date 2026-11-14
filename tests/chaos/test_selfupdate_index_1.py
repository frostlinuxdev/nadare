# SPDX-License-Identifier: GPL-3.0-or-later
"""A malformed release hash from one mirror must not destroy the cached good one."""

from __future__ import annotations

import json

from nadare.core import indexfetch, selfupdate
from nadare.core.updatecheck import UpdateInfo

BASE = "https://index.example/"
INFO = UpdateInfo(
    "0.1.0b20",
    "https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
    "Nadare 0.1.0b20",
)
DIGEST = "a" * 64
GOOD = {
    "schema": 1,
    "releases": {"0.1.0b20": {"x86_64": {"sha256": DIGEST}}},
}
# Same outer shape the validator asks for, but the entry cannot be used.
BAD = {
    "schema": 1,
    "releases": {"0.1.0b20": {"x86_64": {"sha256": "not-a-sha256"}}},
}


def _serve(monkeypatch, payload):
    def opener(url, **_kwargs):
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload).encode()

    monkeypatch.setattr(indexfetch, "open_bytes", opener)


def test_a_malformed_release_entry_does_not_wipe_the_cached_good_hash(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
    _serve(monkeypatch, GOOD)
    first = selfupdate.expected_sha256(
        INFO, fetch_sums=lambda url: "", fetch_index=selfupdate._index_document
    )
    assert first.sha256 == DIGEST

    # One mirror now serves a release entry whose sha256 is nonsense. It passes the
    # document validator today and is written over the good cached document.
    _serve(monkeypatch, BAD)
    try:
        second = selfupdate.expected_sha256(
            INFO, fetch_sums=lambda url: "", fetch_index=selfupdate._index_document
        ).sha256
    except Exception as exc:  # refusal while the bad mirror is up is allowed
        second = f"{type(exc).__name__}: {exc}"
    cached = indexfetch.cache_path(BASE, selfupdate.INDEX_DOC).read_bytes()
    print("with bad mirror:", second)
    print("cached document after bad mirror:", cached)

    # Every mirror is down now. The last good document should still answer.
    _serve(monkeypatch, OSError("offline"))
    try:
        third = selfupdate.expected_sha256(
            INFO, fetch_sums=lambda url: "", fetch_index=selfupdate._index_document
        ).sha256
    except Exception as exc:
        third = f"{type(exc).__name__}: {exc}"
    print("after the bad mirror, offline:", third)
    cached = indexfetch.cache_path(BASE, selfupdate.INDEX_DOC).read_bytes()
    print("cached document offline:", cached)
    assert third == DIGEST
