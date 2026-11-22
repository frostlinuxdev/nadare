# SPDX-License-Identifier: GPL-3.0-or-later
"""Hostile library manifests are ignored without claiming external folders."""

from __future__ import annotations

import json

import pytest

from nadare.core import libraries as lib
from nadare.core.config import Config


@pytest.mark.parametrize(
    "field,value",
    [
        ("install", "../outside"),
        ("install", "/outside"),
        ("install", "."),
        ("install", "bad\x00path"),
        ("install", []),
        ("install", 42),
        ("prefix", "../outside"),
        ("title", []),
        ("backend", {}),
        ("made_by_nadare", {}),
        ("made_by_nadare", ["other"]),
        ("schema", 99),
    ],
)
def test_damaged_manifest_does_not_become_a_candidate(tmp_path, field, value):
    root = tmp_path / "library"
    game = root / "Nadare" / "Game"
    (game / "install").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "save.dat").write_bytes(b"keep")
    data = {
        "schema": 1,
        "backend": "direct-bd2",
        "title": "Game",
        "install": "install",
        "made_by_nadare": ["install"],
    }
    data[field] = value
    (game / lib.MANIFEST_NAME).write_text(json.dumps(data))
    cfg = Config.load(tmp_path / "config.json")
    found, notes = lib.scan(cfg, root)
    assert found == [] and notes
    assert not cfg.profiles
    assert (outside / "save.dat").read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["link", "fifo", "deep_json"])
def test_nonregular_or_recursive_manifest_is_bounded(tmp_path, kind):
    game = tmp_path / "Game"
    game.mkdir()
    path = game / lib.MANIFEST_NAME
    if kind == "link":
        target = tmp_path / "unrelated"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        import os

        os.mkfifo(path)
    else:
        path.write_text("[" * 1500 + "0" + "]" * 1500)
    assert lib.read_manifest(game) is None


def test_valid_manifest_is_still_discovered(tmp_path):
    game = tmp_path / "library" / "Nadare" / "Game"
    (game / "install").mkdir(parents=True)
    (game / lib.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "direct-bd2",
                "title": "Game",
                "install": "install",
                "made_by_nadare": ["install"],
            }
        )
    )
    found, notes = lib.scan(Config.load(tmp_path / "config.json"), tmp_path / "library")
    assert len(found) == 1 and not notes
