# SPDX-License-Identifier: GPL-3.0-or-later
"""RangeReader can check every block it fetches against a published hash list."""

from __future__ import annotations

import hashlib

import pytest
from _httpserver import Server

from nadare.core.rangefile import BlockMismatch, RangePart, RangeReader

BS = 4096
DATA = bytes((i * 7 + (i >> 8)) & 0xFF for i in range(50_000))  # 12 blocks + a short tail


def digests(data: bytes, block_size: int = BS) -> list[bytes]:
    return [
        hashlib.sha256(data[i : i + block_size]).digest() for i in range(0, len(data), block_size)
    ]


class Flaky(dict):
    """An object store whose next ``bad`` GETs of ``path`` return corrupted bytes."""

    def __init__(self, path: str, good: bytes, bad_bytes: bytes, bad: int) -> None:
        super().__init__({path: good})
        self.path, self.bad_bytes, self.bad = path, bad_bytes, bad

    def get(self, key, default=None):
        if key == self.path and self.bad > 0:
            self.bad -= 1
            return self.bad_bytes
        return super().get(key, default)


@pytest.fixture
def server():
    made = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def tampered(data: bytes, at: int) -> bytes:
    return data[:at] + bytes([data[at] ^ 0xFF]) + data[at + 1 :]


def reader(s, *parts, hashes, **kw):
    kw.setdefault("block_size", BS)
    kw.setdefault("backoff", 0)
    return RangeReader(
        [RangePart(s.url + path, size) for path, size in parts], block_hashes=hashes, **kw
    )


def test_a_matching_object_reads_normally(server):
    s = server({"/a": DATA})
    with reader(s, ("/a", len(DATA)), hashes=[digests(DATA)]) as r:
        assert r.read() == DATA


def test_a_tampered_block_is_refused_and_the_good_ones_still_read(server):
    s = server({"/a": tampered(DATA, 10_000)})  # block 2
    with reader(s, ("/a", len(DATA)), hashes=[digests(DATA)]) as r:
        assert r.read(BS) == DATA[:BS]
        r.seek(10_000)
        with pytest.raises(BlockMismatch) as caught:
            r.read(10)
    assert caught.value.part_index == 0
    assert caught.value.block_index == 2
    assert "/a" in str(caught.value)


def test_a_one_off_corrupt_answer_is_refetched_once(server):
    flaky = Flaky("/a", DATA, tampered(DATA, 10_000), bad=1)
    s = server(flaky)
    with reader(s, ("/a", len(DATA)), hashes=[digests(DATA)]) as r:
        r.seek(9_000)
        assert r.read(3_000) == DATA[9_000:12_000]


def test_persistent_corruption_gives_up_after_one_refetch(server):
    s = server({"/a": tampered(DATA, 10_000)})
    with reader(s, ("/a", len(DATA)), hashes=[digests(DATA)]) as r:
        r.seek(10_000)
        with pytest.raises(BlockMismatch):
            r.read(10)
    block_two = [q for q in s.ranges if q[1] == 2 * BS]
    assert len(block_two) == 2, s.ranges  # the first fetch and exactly one more


def test_a_refused_block_is_never_cached(server):
    s = server({"/a": tampered(DATA, 10_000)})
    with reader(s, ("/a", len(DATA)), hashes=[digests(DATA)]) as r:
        r.seek(10_000)
        with pytest.raises(BlockMismatch):
            r.read(10)
        s.objects["/a"] = DATA  # the publisher fixes the file
        r.seek(10_000)
        assert r.read(10) == DATA[10_000:10_010]


def test_split_archives_check_each_part_with_its_own_table(server):
    a, b, c = DATA[:10_001], DATA[10_001:30_000], DATA[30_000:]
    bad_b = tampered(b, len(b) - 1)  # the short last block of the middle part
    s = server({"/a": a, "/b": bad_b, "/c": c})
    hashes = [digests(a), digests(b), digests(c)]
    with reader(s, ("/a", len(a)), ("/b", len(b)), ("/c", len(c)), hashes=hashes) as r:
        assert r.read(len(a)) == a
        r.seek(len(a) + len(b) - 5)
        with pytest.raises(BlockMismatch) as caught:
            r.read(5)
    assert caught.value.part_index == 1
    assert caught.value.block_index == (len(b) - 1) // BS


def test_every_block_of_a_readahead_window_is_checked(server):
    s = server({"/a": tampered(DATA, 10_000)})  # block 2
    with reader(
        s, ("/a", len(DATA)), hashes=[digests(DATA)], readahead=4 * BS, cache_blocks=8
    ) as r:
        with pytest.raises(BlockMismatch) as caught:
            r.read()  # a sequential read ramps up to multi-block requests
    assert caught.value.block_index == 2


def test_a_table_of_the_wrong_length_is_refused_up_front(server):
    s = server({"/a": DATA})
    with pytest.raises(ValueError):
        reader(s, ("/a", len(DATA)), hashes=[digests(DATA)[:-1]])
    with pytest.raises(ValueError):
        reader(s, ("/a", len(DATA)), hashes=[digests(DATA), digests(DATA)])
