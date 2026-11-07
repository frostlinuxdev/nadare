# SPDX-License-Identifier: GPL-3.0-or-later
"""Sophon: a manifest must tile each file, and a cancel is never reported as success."""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("zstandard")

from test_sophon import Build, field_bytes, field_varint, md5, plan_for  # noqa: E402
from test_sophon import servers as servers  # noqa: E402,F401,PLC0414

from nadare.core import directdl as dd  # noqa: E402
from nadare.core import sophon  # noqa: E402
from nadare.core.events import Cancel, Cancelled  # noqa: E402


def _chunk(name: str, offset: int, size: int, data: bytes = b"") -> bytes:
    return field_bytes(
        2,
        field_bytes(1, name)
        + field_bytes(2, md5(data or name.encode()))
        + field_varint(3, offset)
        + field_varint(4, size)
        + field_varint(5, size)
        + field_varint(6, 1)
        + field_bytes(7, md5(name.encode())),
    )


def _file(path: str, size: int, chunks: bytes) -> bytes:
    return field_bytes(
        1,
        field_bytes(1, path)
        + chunks
        + field_varint(4, size)
        + field_bytes(5, hashlib.md5(b"x").hexdigest()),
    )


# ---- #865: chunk coverage ---------------------------------------------------------------
def test_a_file_with_no_chunks_but_a_size_is_refused():
    with pytest.raises(sophon.SophonError, match="cover"):
        sophon.parse_manifest(_file("gap.bin", 4, b""))


def test_a_gap_between_chunks_is_refused():
    chunks = _chunk("a", 0, 4) + _chunk("b", 8, 4)
    with pytest.raises(sophon.SophonError, match="cover"):
        sophon.parse_manifest(_file("gap.bin", 12, chunks))


def test_overlapping_chunks_are_refused():
    chunks = _chunk("a", 0, 6) + _chunk("b", 4, 6)
    with pytest.raises(sophon.SophonError, match="cover"):
        sophon.parse_manifest(_file("o.bin", 10, chunks))


def test_chunks_that_stop_short_of_the_size_are_refused():
    with pytest.raises(sophon.SophonError, match="cover"):
        sophon.parse_manifest(_file("short.bin", 10, _chunk("a", 0, 4)))


def test_a_tiled_file_is_accepted_whatever_order_the_chunks_come_in():
    chunks = _chunk("b", 4, 6) + _chunk("a", 0, 4)
    (entry,) = sophon.parse_manifest(_file("ok.bin", 10, chunks))
    assert entry.size == 10 and len(entry.chunks) == 2


def test_an_empty_file_needs_no_chunks():
    (entry,) = sophon.parse_manifest(_file("empty.bin", 0, b""))
    assert entry.size == 0 and entry.chunks == ()


def test_a_real_shaped_build_still_parses():
    build = Build({"a.bin": b"x" * 250, "sub/b.bin": b"y" * 40, "z.bin": b""}, chunk_size=100)
    assert {e.path for e in sophon.parse_manifest(build.manifest_raw)} == {
        "a.bin",
        "sub/b.bin",
        "z.bin",
    }


# ---- #867: cancel ------------------------------------------------------------------------
def test_a_cancel_during_the_scan_of_installed_files_is_not_success(
    servers, tmp_path, monkeypatch
):
    build = Build({"a.bin": b"x" * 250, "b.bin": b"y" * 250}, chunk_size=100)
    servers(build)
    plan = plan_for(build)
    dd.sync(plan, tmp_path)
    assert sophon.is_done(plan.sophon[0], tmp_path)
    (tmp_path / ".nadare" / "sophon-game.done").unlink()  # sync it again from the files on disk

    cancel = Cancel()
    real = sophon._chunk_matches
    seen = []

    def cancel_on_first(fh, chunk):
        seen.append(chunk.name)
        cancel.cancel()
        return real(fh, chunk)

    monkeypatch.setattr(sophon, "_chunk_matches", cancel_on_first)
    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path, cancel=cancel, workers=1)
    assert not sophon.is_done(plan.sophon[0], tmp_path)
    assert len(seen) < 6, "the scan kept hashing after the cancel"


def test_a_cancel_before_the_manifest_arrives_stops_there(servers, tmp_path):
    build = Build({"a.bin": b"x" * 50}, chunk_size=100)
    srv = servers(build)
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        dd.sync(plan_for(build), tmp_path, cancel=cancel)
    assert srv.requests == []
