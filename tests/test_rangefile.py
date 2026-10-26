# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.rangefile against a real local HTTP server."""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from _httpserver import Server

from nadare.core.events import Cancel, Cancelled
from nadare.core.rangefile import RangeError, RangePart, RangeReader

DATA = bytes((i * 7 + (i >> 8)) & 0xFF for i in range(200_000))


@pytest.fixture
def server():
    made = []

    def make(objects, **kw):
        s = Server(objects, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def reader(s, *names, **kw):
    kw.setdefault("block_size", 4096)
    kw.setdefault("backoff", 0)
    return RangeReader([s.url + n for n in names], **kw)


def test_whole_object_reads_and_size_comes_from_head(server):
    s = server({"/a": DATA})
    with reader(s, "/a") as r:
        assert r.size == len(DATA)
        assert r.read() == DATA
        assert r.read(10) == b""  # at the end
    assert "HEAD /a" in s.requests


def test_known_sizes_skip_the_head_request(server):
    s = server({"/a": DATA})
    with RangeReader([RangePart(s.url + "/a", len(DATA))], block_size=4096) as r:
        assert r.read(5) == DATA[:5]
    assert not any(q.startswith("HEAD") for q in s.requests)


@pytest.mark.parametrize(
    "pos,n", [(0, 1), (4095, 2), (4096, 4096), (123_456, 30_000), (199_990, 100)]
)
def test_seek_and_read_at_arbitrary_places(server, pos, n):
    s = server({"/a": DATA})
    with reader(s, "/a") as r:
        r.seek(pos)
        assert r.read(n) == DATA[pos : pos + n]
        assert r.tell() == min(pos + n, len(DATA))


def test_seek_whence_and_errors(server):
    s = server({"/a": DATA})
    with reader(s, "/a") as r:
        assert r.seek(-10, io.SEEK_END) == len(DATA) - 10
        assert r.read() == DATA[-10:]
        assert r.seek(5) == 5 and r.seek(5, io.SEEK_CUR) == 10
        with pytest.raises(ValueError):
            r.seek(-1)
        assert r.seek(len(DATA) + 50) == len(DATA) + 50 and r.read(4) == b""


def test_parts_are_read_as_one_file_across_boundaries(server):
    a, b, c = DATA[:50_000], DATA[50_000:120_001], DATA[120_001:]
    s = server({"/a": a, "/b": b, "/c": c})
    with reader(s, "/a", "/b", "/c") as r:
        assert r.size == len(DATA)
        for pos in (49_990, 50_000, 120_000, 0):
            r.seek(pos)
            assert r.read(30_000) == DATA[pos : pos + 30_000]
        r.seek(0)
        assert r.read() == DATA


def test_sequential_small_reads_become_few_large_requests(server):
    s = server({"/a": DATA})
    with reader(s, "/a", block_size=16_384) as r:
        got = b"".join(iter(lambda: r.read(100), b""))
    assert got == DATA
    assert r.requests == -(-len(DATA) // 16_384)  # one per block, no more
    assert r.bytes_fetched == len(DATA) and s.bytes_served == len(DATA)


def test_recent_blocks_are_cached(server):
    s = server({"/a": DATA})
    with reader(s, "/a", block_size=16_384) as r:
        r.read(10)
        r.seek(0)
        r.read(10)
        r.seek(5)
        r.read(10)
        assert r.requests == 1


def test_a_server_without_range_support_is_reported(server):
    s = server({"/a": DATA})
    s.ignore_range = True
    with reader(s, "/a") as r, pytest.raises(RangeError, match="(?i)range"):
        r.read(10)


def test_flaky_responses_are_retried(server):
    s = server({"/a": DATA})
    with reader(s, "/a", retries=3) as r:
        assert r.size  # HEAD happens at construction
        s.fail_next = 2
        assert r.read(100) == DATA[:100]


def test_gives_up_after_the_retries(server):
    s = server({"/a": DATA})
    with reader(s, "/a", retries=2) as r:
        s.fail_next = 10
        with pytest.raises(RangeError):
            r.read(100)


def test_a_missing_object_fails_at_once(server):
    s = server({})
    with pytest.raises(RangeError):
        reader(s, "/nope")


def test_headers_are_sent_on_every_request(server):
    s = server({"/a": DATA})
    with RangeReader(
        [s.url + "/a"], headers={"Referer": "https://x.example/"}, block_size=4096
    ) as r:
        r.read(10)
    assert all(h.get("referer") == "https://x.example/" for h in s.headers)


def test_cancel_is_checked_before_a_request(server):
    s = server({"/a": DATA})
    cancel = Cancel()
    with reader(s, "/a", cancel=cancel) as r:
        r.read(10)
        cancel.cancel()
        r.seek(100_000)
        with pytest.raises(Cancelled):
            r.read(10)


def test_closed_reader_raises(server):
    s = server({"/a": DATA})
    r = reader(s, "/a")
    r.close()
    with pytest.raises(ValueError):
        r.read(1)
    r.close()  # idempotent


def test_zipfile_reads_members_through_it_with_few_requests(server):
    blobs = {
        f"dir/file{i}.bin": os.urandom(40_000) if i % 2 else bytes([i]) * 40_000 for i in range(10)
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in blobs.items():
            z.writestr(name, data)
    zdata = buf.getvalue()
    s = server({"/z.zip": zdata})
    with reader(s, "/z.zip", block_size=65_536) as r, zipfile.ZipFile(r) as zf:
        assert sorted(zf.namelist()) == sorted(blobs)
        for name, data in blobs.items():
            assert zf.read(name) == data
        wanted = zf.getinfo("dir/file3.bin")
    assert wanted.CRC
    assert r.bytes_fetched <= len(zdata) + 65_536  # roughly the archive, read once


def test_a_split_zip_reads_like_a_whole_one(server):
    blobs = {f"f{i}.txt": (f"line {i}\n" * 3000).encode() for i in range(8)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in blobs.items():
            z.writestr(name, data)
    z = buf.getvalue()
    cut = [0, len(z) // 3, 2 * len(z) // 3, len(z)]
    s = server({f"/p{i}": z[cut[i] : cut[i + 1]] for i in range(3)})
    with reader(s, "/p0", "/p1", "/p2", block_size=8192) as r, zipfile.ZipFile(r) as zf:
        assert all(zf.read(n) == d for n, d in blobs.items())


# --- speed: keep-alive and read-ahead -------------------------------------------------
BIG = bytes((i * 31 + (i >> 9)) & 0xFF for i in range(1_000_000))


def test_one_connection_serves_many_blocks(server):
    s = server({"/a": BIG}, keepalive=True)
    with reader(s, "/a", block_size=4096) as r:
        for pos in range(0, 40 * 4096, 4096 * 3):  # non-sequential: 14 separate blocks
            r.seek(pos)
            assert r.read(10) == BIG[pos : pos + 10]
        assert r.requests >= 10
    assert s.connections <= 2  # HEAD may use one; every ranged GET shares one


def test_sequential_reads_grow_into_large_requests(server):
    s = server({"/a": BIG}, keepalive=True)
    with RangeReader(
        [RangePart(s.url + "/a", len(BIG))], block_size=4096, readahead=256 * 1024, backoff=0
    ) as r:
        got = b"".join(iter(lambda: r.read(1000), b""))
    assert got == BIG
    assert len(s.ranges) <= 12  # ramps 1,2,4.. blocks up to 256 KB: ~10 requests, not 245
    assert max(e - st + 1 for _, st, e in s.ranges) <= 256 * 1024
    assert s.connections <= 2


def test_random_access_does_not_read_ahead(server):
    s = server({"/a": BIG}, keepalive=True)
    with RangeReader(
        [RangePart(s.url + "/a", len(BIG))], block_size=4096, readahead=256 * 1024, backoff=0
    ) as r:
        for pos in (500_000, 100_000, 900_000, 10_000):
            r.seek(pos)
            assert r.read(10) == BIG[pos : pos + 10]
    assert all(e - st + 1 == 4096 for _, st, e in s.ranges)


def test_readahead_stops_at_the_part_boundary(server):
    a, b = BIG[:300_000], BIG[300_000:]
    s = server({"/a": a, "/b": b}, keepalive=True)
    with RangeReader(
        [RangePart(s.url + "/a", len(a)), RangePart(s.url + "/b", len(b))],
        block_size=4096,
        readahead=256 * 1024,
        backoff=0,
    ) as r:
        assert r.read() == BIG
    for path, _start, e in s.ranges:
        assert e < {"/a": len(a), "/b": len(b)}[path]


def test_readahead_still_survives_a_flaky_server(server):
    s = server({"/a": BIG}, keepalive=True)
    s.fail_next = 2
    with RangeReader(
        [RangePart(s.url + "/a", len(BIG))], block_size=4096, readahead=64 * 1024, backoff=0
    ) as r:
        assert r.read() == BIG
