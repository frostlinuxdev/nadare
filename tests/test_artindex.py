# SPDX-License-Identifier: GPL-3.0-or-later
"""Curated cover art from the index: what the app accepts, and how a cover gets upgraded."""

from __future__ import annotations

import time

import pytest

from nadare.core import artindex

GOOD_URL = "https://cdn2.unrealengine.com/egs-genshinimpact-x-s1-2560x1440-abc.jpg"
STEAM = "https://cdn.cloudflare.steamstatic.com/steam/apps/3513350/library_hero.jpg"


def _doc(**games):
    return {"schema": 1, "updated": "2026-10-28", "games": games}


def _entry(url=GOOD_URL, **extra):
    return {"url": url, "orientation": "wide", "width": 2560, "height": 1440, **extra}


@pytest.fixture(autouse=True)
def fresh():
    artindex.reset()
    yield
    artindex.reset()


# ---- what is accepted ---------------------------------------------------------------------
def test_a_good_document_is_parsed_into_candidates_per_game():
    parsed = artindex.parse(_doc(**{"direct-genshin": {"cover": [_entry()]}}))
    assert [c.url for c in parsed["direct-genshin"]] == [GOOD_URL]
    assert parsed["direct-genshin"][0].width == 2560


@pytest.mark.parametrize(
    "doc",
    [
        None,
        [],
        {"schema": 2, "games": {}},
        {"schema": 1},
        {"schema": 1, "games": []},
        {"games": {}},
    ],
)
def test_a_document_of_the_wrong_shape_yields_nothing(doc):
    assert artindex.parse(doc) == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn2.unrealengine.com/a.jpg",  # not https
        "https://evil.example/a.jpg",  # host not allowed for this game
        "https://cdn2.unrealengine.com:8443/a.jpg",  # explicit port
        "https://user:pw@cdn2.unrealengine.com/a.jpg",  # userinfo
        "https://127.0.0.1/a.jpg",
        "//cdn2.unrealengine.com/a.jpg",
        "",
        None,
        42,
    ],
)
def test_a_candidate_on_a_bad_url_is_dropped(url):
    parsed = artindex.parse(_doc(**{"direct-genshin": {"cover": [_entry(url=url)]}}))
    assert parsed == {}


def test_the_index_cannot_widen_the_host_list_of_a_game():
    # Steam is allowed for Wuthering Waves but not for Genshin.
    parsed = artindex.parse(
        _doc(
            **{
                "direct-genshin": {"cover": [_entry(url=STEAM)]},
                "direct-wuwa": {"cover": [_entry(url=STEAM)]},
            }
        )
    )
    assert "direct-genshin" not in parsed and "direct-wuwa" in parsed


def test_an_unknown_game_is_ignored():
    assert artindex.parse(_doc(**{"no-such-kind": {"cover": [_entry()]}})) == {}


def test_a_game_keeps_at_most_four_candidates_in_order():
    entries = [_entry(url=f"https://cdn2.unrealengine.com/{i}.jpg") for i in range(7)]
    parsed = artindex.parse(_doc(**{"direct-genshin": {"cover": entries}}))
    assert [c.url.rsplit("/", 1)[1] for c in parsed["direct-genshin"]] == [
        "0.jpg",
        "1.jpg",
        "2.jpg",
        "3.jpg",
    ]


@pytest.mark.parametrize(
    "bad",
    [{"width": "big"}, {"width": -1}, {"width": 0}, {"height": 10**7}, {"orientation": "square"}],
)
def test_implausible_dimensions_or_orientation_drop_the_candidate(bad):
    entry = {**_entry(), **bad}
    assert artindex.parse(_doc(**{"direct-genshin": {"cover": [entry]}})) == {}


def test_one_bad_candidate_does_not_hide_a_good_one_behind_it():
    parsed = artindex.parse(
        _doc(**{"direct-genshin": {"cover": [_entry(url="http://x/a.jpg"), _entry()]}})
    )
    assert [c.url for c in parsed["direct-genshin"]] == [GOOD_URL]


# ---- looking it up -------------------------------------------------------------------------
def _loader(doc, calls):
    def load():
        calls.append(1)
        return doc

    return load


def test_waiting_lookup_loads_the_document_once_and_serves_candidates():
    calls = []
    artindex.set_loader(_loader(_doc(**{"direct-genshin": {"cover": [_entry()]}}), calls))
    first = artindex.lookup("direct-genshin", wait=True)
    second = artindex.lookup("direct-genshin", wait=True)
    assert first == second == [(GOOD_URL, ("cdn2.unrealengine.com",))]
    assert len(calls) == 1


def test_a_non_waiting_lookup_never_blocks_and_fills_in_for_the_next_one():
    calls = []
    gate = []

    def slow():
        calls.append(1)
        while not gate:
            time.sleep(0.01)
        return _doc(**{"direct-genshin": {"cover": [_entry()]}})

    artindex.set_loader(slow)
    try:
        started = time.monotonic()
        assert artindex.lookup("direct-genshin", wait=False) == []
        assert time.monotonic() - started < 0.5
    finally:
        gate.append(1)
    artindex.wait_for_load(5)
    assert artindex.lookup("direct-genshin", wait=False) == [(GOOD_URL, ("cdn2.unrealengine.com",))]


def test_an_unavailable_index_gives_no_candidates_and_is_not_retried_at_once():
    calls = []

    def boom():
        calls.append(1)
        raise OSError("offline")

    artindex.set_loader(boom)
    assert artindex.lookup("direct-genshin", wait=True) == []
    assert artindex.lookup("direct-genshin", wait=True) == []
    assert len(calls) == 1


def test_a_game_the_index_does_not_know_has_no_candidates():
    artindex.set_loader(_loader(_doc(**{"direct-genshin": {"cover": [_entry()]}}), []))
    assert artindex.lookup("direct-bd2", wait=True) == []


# ---- the shipped seed document -------------------------------------------------------------
def test_the_seed_document_passes_the_same_validation():
    import json
    from pathlib import Path

    seed = json.loads(
        (Path(__file__).parents[1] / "docs" / "art-index.seed.json").read_text("utf-8")
    )
    parsed = artindex.parse(seed)
    for kind, entries in seed["games"].items():
        assert parsed.get(kind), f"{kind}: no accepted candidate in the seed"
        assert len(parsed[kind]) == len(entries["cover"]), f"{kind}: a seed candidate is rejected"


# ---- review fixes ------------------------------------------------------------------------------
def test_a_boolean_schema_is_not_schema_one():
    assert (
        artindex.parse({"schema": True, "games": {"direct-genshin": {"cover": [_entry()]}}}) == {}
    )


def test_the_saved_copy_is_used_at_once_and_refreshed_in_the_background(monkeypatch):
    """A fresh process must not need the network to know the art list it saw last time."""
    from nadare.core import indexfetch

    saved = _doc(**{"direct-genshin": {"cover": [_entry()]}})
    monkeypatch.setattr(indexfetch, "read_cached", lambda rel, validate=None: saved)
    gate = []

    def slow():
        while not gate:
            time.sleep(0.01)
        return _doc()

    artindex.set_loader(slow)
    assert artindex.lookup("direct-genshin", wait=False) == [(GOOD_URL, ("cdn2.unrealengine.com",))]
    gate.append(1)
    artindex.wait_for_load(5)


def test_a_missing_saved_copy_gives_nothing_without_waiting(monkeypatch):
    from nadare.core import indexfetch

    monkeypatch.setattr(indexfetch, "read_cached", lambda rel, validate=None: None)
    artindex.set_loader(lambda: _doc())
    assert artindex.lookup("direct-genshin", wait=False) == []
    artindex.wait_for_load(5)


def test_a_load_that_finishes_after_the_state_was_cleared_is_ignored():
    gate = []
    started = []

    def slow():
        started.append(1)
        while not gate:
            time.sleep(0.01)
        return _doc(**{"direct-genshin": {"cover": [_entry()]}})

    artindex.set_loader(slow)
    try:
        artindex.lookup("direct-genshin", wait=False)  # a background load is now running
        deadline = time.monotonic() + 3
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started, "the background load should have begun"
        artindex._invalidate()  # what reset() does to the state, without waiting for the load
    finally:
        gate.append(1)  # always release the loader, or later tests would wait on it
    artindex.wait_for_load(5)
    assert artindex._fresh() is None


def test_a_failed_refresh_keeps_answering_from_the_saved_copy(monkeypatch):
    """An outage must not throw away the copy saved on disk for the retry window."""
    from nadare.core import indexfetch

    saved = _doc(**{"direct-genshin": {"cover": [_entry()]}})
    monkeypatch.setattr(indexfetch, "read_cached", lambda rel, validate=None: saved)

    def boom():
        raise OSError("offline")

    artindex.set_loader(boom)
    assert artindex.lookup("direct-genshin", wait=False) == [(GOOD_URL, ("cdn2.unrealengine.com",))]
    artindex.wait_for_load(5)
    assert artindex.lookup("direct-genshin", wait=False) == [(GOOD_URL, ("cdn2.unrealengine.com",))]


def test_a_failed_first_load_falls_back_to_the_saved_copy(monkeypatch):
    from nadare.core import indexfetch

    saved = _doc(**{"direct-genshin": {"cover": [_entry()]}})
    monkeypatch.setattr(indexfetch, "read_cached", lambda rel, validate=None: saved)

    def boom():
        raise OSError("offline")

    artindex.set_loader(boom)
    assert artindex.lookup("direct-genshin", wait=True) == [(GOOD_URL, ("cdn2.unrealengine.com",))]


def test_the_index_hooks_do_not_accept_a_schema_they_do_not_understand(monkeypatch):
    from nadare.core import indexfetch

    validators = []

    def fake_fetch_json(rel, *, validate=None, **kwargs):
        validators.append(validate)
        return indexfetch.IndexResult({}, False, "")

    monkeypatch.setattr(indexfetch, "fetch_json", fake_fetch_json)
    artindex._default_loader()
    monkeypatch.setattr(
        indexfetch, "read_cached", lambda rel, validate=None: validators.append(validate)
    )
    artindex._saved_copy()
    assert len(validators) == 2 and all(callable(v) for v in validators)
    for validate in validators:
        assert validate({"schema": 2, "games": {}}) is False
        assert validate({"schema": True, "games": {}}) is False
        assert validate(_doc(**{"direct-genshin": {"cover": [_entry()]}})) is True


def test_an_unreadable_schema_never_overwrites_the_saved_copy(monkeypatch):
    """A document the app cannot use must not destroy the last good cached copy."""
    import json

    from nadare.core import indexfetch

    monkeypatch.setenv("NADARE_INDEX_BASE", "https://a.example/index/")
    good = _doc(**{"direct-genshin": {"cover": [_entry()]}})
    validate = artindex.valid_document
    indexfetch.fetch_json(
        "catalog/art.json", validate=validate, opener=lambda url: json.dumps(good).encode()
    )
    fallback = indexfetch.fetch_json(
        "catalog/art.json",
        validate=validate,
        opener=lambda url: json.dumps({"schema": 2, "games": {}}).encode(),
    )
    assert fallback.data == good and fallback.from_cache is True
    assert indexfetch.read_cached("catalog/art.json", validate=validate) == good
