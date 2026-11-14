# SPDX-License-Identifier: GPL-3.0-or-later
"""A schema-1 art document of the wrong shape must not replace the cached good list."""

from __future__ import annotations

import json

from nadare.core import artindex, indexfetch

BASE = "https://index.example/"
GOOD_URL = "https://cdn2.unrealengine.com/egs-genshinimpact-x-s1-2560x1440-abc.jpg"
HOSTS = ("cdn2.unrealengine.com",)
GOOD = {
    "schema": 1,
    "games": {
        "direct-genshin": {
            "cover": [{"url": GOOD_URL, "orientation": "wide", "width": 2560, "height": 1440}]
        }
    },
}
BAD = {"schema": 1, "games": []}  # right schema, unusable shape


def _serve(monkeypatch, payload):
    def opener(url, **_kwargs):
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload).encode()

    monkeypatch.setattr(indexfetch, "open_bytes", opener)


def test_a_wrong_shape_art_document_does_not_wipe_the_cached_good_one(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", BASE)
    artindex.reset()

    _serve(monkeypatch, GOOD)
    assert artindex.lookup("direct-genshin", wait=True) == [(GOOD_URL, HOSTS)]
    good_cache = indexfetch.cache_path(BASE, "catalog/art.json").read_bytes()
    print("cached good art document:", good_cache)

    # A mirror answers with schema 1 but no usable games mapping. The validator only
    # looks at the schema number, so this is cached over the good document.
    artindex.reset()
    _serve(monkeypatch, BAD)
    print("lookup while bad mirror is up:", artindex.lookup("direct-genshin", wait=True))
    bad_cache = indexfetch.cache_path(BASE, "catalog/art.json").read_bytes()
    print("cached document after bad mirror:", bad_cache)

    # Now everything is offline: the last good document should still supply the cover.
    artindex.reset()
    _serve(monkeypatch, OSError("offline"))
    got = artindex.lookup("direct-genshin", wait=True)
    print("offline lookup after bad mirror:", got)
    print("cached document offline:", indexfetch.cache_path(BASE, "catalog/art.json").read_bytes())
    assert got == [(GOOD_URL, HOSTS)]


def test_an_entry_without_covers_does_not_reject_the_whole_document():
    """A newer index may carry other art kinds; parse skips such an entry, so must the check."""
    from nadare.core import artindex

    games = {"direct-bd2": {"hero": []}, "direct-hsr": {"cover": []}}
    assert artindex.valid_document({"schema": artindex.SCHEMA, "games": games})
    broken = {"direct-hsr": {"cover": "x"}}
    assert not artindex.valid_document({"schema": artindex.SCHEMA, "games": broken})
