# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit low #602: the Yostar index hash and the manifest schema are checked."""

from __future__ import annotations

import hashlib
import json

import pytest

from nadare.core import yostar


def _manifest(**over):
    base = {
        "game_tag": "Game_EN",
        "version": "1.0.0",
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [{"path": "a.exe", "size": 3, "crc64": yostar.crc64(b"abc")}],
    }
    base.update(over)
    return base


def _index(tmp_path, manifest_bytes, *, sha256="auto", name="Game_EN.json"):
    (tmp_path / name).write_bytes(manifest_bytes)
    entry = {"version": "1.0.0", "url": name}
    if sha256 == "auto":
        entry["sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    elif sha256 is not None:
        entry["sha256"] = sha256
    (tmp_path / "index.json").write_text(json.dumps({"Game_EN": entry}), encoding="utf-8")
    return str(tmp_path / "index.json")


def _body(**over) -> bytes:
    return json.dumps(_manifest(**over)).encode("utf-8")


def test_a_manifest_matching_the_index_hash_loads(tmp_path):
    source = _index(tmp_path, _body())
    assert yostar.load_manifest(source, game_tag="Game_EN").game_tag == "Game_EN"


def test_a_manifest_that_does_not_match_the_index_hash_is_refused(tmp_path):
    source = _index(tmp_path, _body(), sha256="0" * 64)
    with pytest.raises(yostar.ManifestError, match="does not match"):
        yostar.load_manifest(source, game_tag="Game_EN")


def test_the_hash_comparison_ignores_case(tmp_path):
    body = _body()
    source = _index(tmp_path, body, sha256=hashlib.sha256(body).hexdigest().upper())
    assert yostar.load_manifest(source, game_tag="Game_EN").version == "1.0.0"


def test_an_index_entry_without_a_hash_still_loads(tmp_path):
    source = _index(tmp_path, _body(), sha256=None)
    assert yostar.load_manifest(source, game_tag="Game_EN").version == "1.0.0"


@pytest.mark.parametrize("bad", ["not-a-hash", "abc", 5, ""])
def test_a_malformed_index_hash_is_ignored_not_trusted(tmp_path, bad):
    source = _index(tmp_path, _body(), sha256=bad)
    assert yostar.load_manifest(source, game_tag="Game_EN").version == "1.0.0"


def test_a_manifest_without_a_schema_field_is_schema_one(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(_body())
    assert yostar.load_manifest(str(path)).schema == 1


def test_a_future_schema_is_refused(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(_body(schema=99))
    with pytest.raises(yostar.ManifestError, match="schema"):
        yostar.load_manifest(str(path))


@pytest.mark.parametrize("bad", ["1", 1.5, None, True, -1, 0])
def test_a_nonsense_schema_is_refused(tmp_path, bad):
    path = tmp_path / "m.json"
    path.write_bytes(_body(schema=bad))
    with pytest.raises(yostar.ManifestError, match="schema"):
        yostar.load_manifest(str(path))


def test_the_current_schema_is_accepted(tmp_path):
    path = tmp_path / "m.json"
    path.write_bytes(_body(schema=yostar.SCHEMA))
    assert yostar.load_manifest(str(path)).schema == yostar.SCHEMA
