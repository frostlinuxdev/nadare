# SPDX-License-Identifier: GPL-3.0-or-later
"""The block-pin document: build, parse, match and load."""

from __future__ import annotations

import base64
import hashlib

import pytest

from nadare.core import archivepins, directdl, indexfetch

BS = 65536
DATA = bytes(range(256)) * 700  # 179,200 bytes: 2 full blocks and a short third


def pin(url: str = "https://cdn.example/g.zip", data: bytes = DATA, md5: str | None = None):
    return archivepins.pin_from_bytes(url, data, BS, publisher_md5=md5)


def test_hash_blocks_matches_a_manual_hash_and_keeps_the_short_tail():
    blocks = archivepins.hash_blocks(DATA, BS)
    assert len(blocks) == 3
    assert blocks[0] == hashlib.sha256(DATA[:BS]).digest()
    assert blocks[2] == hashlib.sha256(DATA[2 * BS :]).digest()
    assert archivepins.hash_blocks(b"", BS) == []


def test_a_document_round_trips():
    original = pin(md5="ab" * 16)
    doc = archivepins.document({"g": original})
    parsed = archivepins.parse_document(doc)
    assert parsed == {"g": original}
    assert doc["schema"] == 1
    assert doc["archives"]["g"]["block_size"] == BS


def test_the_published_form_is_compact_base64():
    doc = archivepins.document({"g": pin()})
    part = doc["archives"]["g"]["parts"][0]
    assert part["size"] == len(DATA)
    assert len(base64.b64decode(part["blocks"])) == 3 * 32


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema=2),
        lambda d: d["archives"]["g"].update(block_size=1000),  # not a power of two
        lambda d: d["archives"]["g"].update(block_size=1 << 30),  # absurdly large
        lambda d: d["archives"]["g"]["parts"][0].update(blocks=""),  # wrong table length
        lambda d: d["archives"]["g"]["parts"][0].update(blocks="!!!not base64!!!"),
        lambda d: d["archives"]["g"]["parts"][0].update(size=-1),
        lambda d: d["archives"]["g"]["parts"][0].update(url="ftp://x/g.zip"),
        lambda d: d["archives"]["g"].update(parts=[]),
        lambda d: d.update(archives=["not", "a", "mapping"]),
    ],
)
def test_a_malformed_document_is_refused(mutate):
    doc = archivepins.document({"g": pin()})
    mutate(doc)
    with pytest.raises(archivepins.PinError):
        archivepins.parse_document(doc)


def parts_of(pin_obj, **override):
    return [
        directdl.RemoteFile(
            "g.zip", override.get("url", p.url), override.get("size", p.size), md5=None
        )
        for p in pin_obj.parts
    ]


def test_a_pin_applies_only_to_the_exact_archive():
    p = pin()
    assert p.matches(parts_of(p), None)
    assert not p.matches(parts_of(p, url="https://cdn.example/other.zip"), None)
    assert not p.matches(parts_of(p, size=len(DATA) + 1), None)
    assert not p.matches(parts_of(p) * 2, None)  # a different number of parts


def test_the_publisher_hash_must_agree_when_both_sides_have_one():
    p = pin(md5="ab" * 16)
    assert p.matches(parts_of(p), "AB" * 16)  # case-insensitive
    assert not p.matches(parts_of(p), "cd" * 16)
    assert p.matches(parts_of(p), None)  # the publisher gave none: size and URL decide
    assert pin().matches(parts_of(pin()), "cd" * 16)  # the pin has none: same


def plan_for(url: str, size: int):
    source = directdl.RemoteFile("g.zip", url, size)
    return directdl.Plan("T", "1", archives=(directdl.Archive("g", source, stream=True),))


def test_attach_pins_only_to_archives_that_match():
    p = pin()
    attached = archivepins.attach(plan_for(p.parts[0].url, len(DATA)), {"g": p})
    assert attached.archives[0].pin == p
    unmatched = archivepins.attach(plan_for(p.parts[0].url, len(DATA) + 1), {"g": p})
    assert unmatched.archives[0].pin is None
    other_name = archivepins.attach(plan_for(p.parts[0].url, len(DATA)), {"x": p})
    assert other_name.archives[0].pin is None


def test_load_returns_the_pins_from_the_index(monkeypatch):
    doc = archivepins.document({"g": pin()})
    seen = {}

    def fake_fetch(rel, **kw):
        seen["rel"] = rel
        seen["max_bytes"] = kw.get("max_bytes")
        return indexfetch.IndexResult(doc, False, "mirror")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", fake_fetch)
    assert archivepins.load("gfl2") == {"g": pin()}
    assert seen["rel"] == "catalog/archive-pins/gfl2.json"
    assert seen["max_bytes"] and seen["max_bytes"] > 64 * 1024  # a pin file is bigger than 64 KiB


def test_no_document_or_a_bad_one_means_no_pins_and_never_an_error(monkeypatch):
    def unavailable(rel, **kw):
        raise indexfetch.IndexUnavailable("down")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", unavailable)
    assert archivepins.load("gfl2") == {}

    monkeypatch.setattr(
        archivepins.indexfetch,
        "fetch_json",
        lambda rel, **kw: indexfetch.IndexResult({"schema": 99}, False, "m"),
    )
    assert archivepins.load("gfl2") == {}


def test_a_game_tag_cannot_escape_the_pin_folder(monkeypatch):
    called = []
    monkeypatch.setattr(
        archivepins.indexfetch, "fetch_json", lambda rel, **kw: called.append(rel) or None
    )
    assert archivepins.load("../../etc/passwd") == {}
    assert called == []


def test_a_pin_document_uses_a_short_cache_so_a_retracted_pin_does_not_linger(monkeypatch):
    seen = {}

    def fake_fetch(rel, **kw):
        seen.update(kw)
        return indexfetch.IndexResult(archivepins.document({"g": pin()}), False, "m")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", fake_fetch)
    archivepins.load("gfl2")
    assert seen["max_cache_age"] <= 3 * 24 * 3600  # not the generic 30 days


def test_fetch_json_ignores_a_saved_copy_older_than_the_caller_allows(monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setenv("NADARE_INDEX_BASE", "https://index.example/")
    rel = "catalog/archive-pins/x.json"
    saved = indexfetch.cache_path("https://index.example/", rel)
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text('{"ok": 1}', encoding="utf-8")
    old = time.time() - 5 * 24 * 3600
    os.utime(saved, (old, old))

    def down(url):
        raise OSError("down")

    assert indexfetch.fetch_json(rel, opener=down).data == {"ok": 1}  # generic 30-day window
    with pytest.raises(indexfetch.IndexUnavailable):
        indexfetch.fetch_json(rel, opener=down, max_cache_age=3 * 24 * 3600)


def test_a_pin_attaches_when_the_plan_has_no_size_for_the_archive():
    """Aether Gazer and GFL2 sources do not know the archive size; the pin supplies it."""
    p = pin()
    parts = [directdl.RemoteFile("g.zip", p.parts[0].url, None)]
    assert p.matches(parts, None)
    source = directdl.RemoteFile("g.zip", p.parts[0].url, None)
    plan = directdl.Plan("T", "1", archives=(directdl.Archive("g", source, stream=True),))
    assert archivepins.attach(plan, {"g": p}).archives[0].pin == p


def test_two_mirrors_a_malformed_first_document_does_not_hide_a_valid_second(monkeypatch):
    good = archivepins.document({"g": pin()})
    served = {"https://m1.example/": b'{"schema": 2, "archives": {}}', "https://m2.example/": None}
    served["https://m2.example/"] = __import__("json").dumps(good).encode()
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://m1.example/ https://m2.example/")

    def opener(url):
        for base, payload in served.items():
            if url.startswith(base):
                return payload
        raise OSError("unknown host")

    real_fetch_json = indexfetch.fetch_json
    monkeypatch.setattr(
        archivepins.indexfetch,
        "fetch_json",
        lambda rel, **kw: real_fetch_json(rel, opener=opener, **kw),
    )
    assert archivepins.load("g") == {"g": pin()}


def test_the_fingerprint_changes_when_any_block_changes():
    a = pin()
    other = bytearray(DATA)
    other[100] ^= 0xFF
    b = pin(data=bytes(other))
    assert a.fingerprint() == pin().fingerprint()
    assert a.fingerprint() != b.fingerprint()


@pytest.fixture(autouse=True)
def _fresh_pin_cache():
    archivepins.clear_cache()
    yield
    archivepins.clear_cache()


def test_one_operation_fetches_a_games_pin_document_once(monkeypatch):
    calls = []

    def fake_fetch(rel, **kw):
        calls.append(rel)
        return indexfetch.IndexResult(archivepins.document({"g": pin()}), False, "m")

    monkeypatch.setattr(archivepins.indexfetch, "fetch_json", fake_fetch)
    for _ in range(3):  # check_update, sync and verify each build a plan
        assert archivepins.load("gfl2") == {"g": pin()}
    assert len(calls) == 1
    archivepins.load("other")
    assert len(calls) == 2  # another game is its own document


def test_the_pin_cache_expires_so_a_new_pin_is_picked_up(monkeypatch):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(archivepins.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        archivepins.indexfetch,
        "fetch_json",
        lambda rel, **kw: calls.append(rel)
        or indexfetch.IndexResult(archivepins.document({}), False, "m"),
    )
    archivepins.load("gfl2")
    now[0] += archivepins.PIN_MEMO_SECONDS + 1
    archivepins.load("gfl2")
    assert len(calls) == 2
