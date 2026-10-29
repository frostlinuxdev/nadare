# SPDX-License-Identifier: GPL-3.0-or-later
"""Staging and ``.done`` temp names must never be followed through a planted symlink."""

from __future__ import annotations

import json
from pathlib import Path

from nadare.core import directdl as dd
from nadare.core import sevenz

FIX = Path(__file__).parent / "fixtures" / "sevenz"


def open7z(name: str):
    return open(FIX / f"{name}.7z", "rb")


def assert_regular(path: Path) -> None:
    assert path.is_file()
    assert not path.is_symlink()


def test_sevenz_extraction_does_not_follow_a_planted_staging_symlink(tmp_path):
    dest = tmp_path / "install"
    dest.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"KEEP")
    (dest / "hello.txt.nadare-new").symlink_to(victim)

    with open7z("lzma2") as fh:
        sevenz.extract(fh, dest)

    assert victim.read_bytes() == b"KEEP"
    assert_regular(dest / "hello.txt")
    assert (dest / "hello.txt").read_bytes() == (FIX / "expected" / "hello.txt").read_bytes()
    assert not list(dest.rglob("*.nadare-new"))


def test_sevenz_extraction_replaces_a_stale_regular_staging_file(tmp_path):
    dest = tmp_path / "install"
    dest.mkdir()
    stale = dest / "hello.txt.nadare-new"
    stale.write_bytes(b"left over from a crash")

    with open7z("lzma2") as fh:
        sevenz.extract(fh, dest)

    assert_regular(dest / "hello.txt")
    assert (dest / "hello.txt").read_bytes() == (FIX / "expected" / "hello.txt").read_bytes()
    assert not list(dest.rglob("*.nadare-new"))


def test_done_record_does_not_follow_a_planted_temp_symlink(tmp_path):
    dest = tmp_path / "install"
    state = dest / dd.STATE_DIR
    state.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"KEEP")
    (state / "game.done.tmp").symlink_to(victim)

    plan = dd.Plan("T", "1")
    archive = dd.Archive("game", dd.RemoteFile("game.zip", "http://example.invalid/game.zip", 1))
    dd._write_done(plan, archive, dest)

    assert victim.read_bytes() == b"KEEP"
    record_path = state / "game.done"
    assert_regular(record_path)
    assert json.loads(record_path.read_text(encoding="utf-8")) == dd._done_record(plan, archive)
    assert not (state / "game.done.tmp").exists()
