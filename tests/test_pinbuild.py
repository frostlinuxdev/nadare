# SPDX-License-Identifier: GPL-3.0-or-later
"""Building a block pin from a streamed archive (no network: the chunks are injected)."""

from __future__ import annotations

import hashlib

import pytest

from nadare.core import archivepins, pinbuild

URL = "https://cdn.example/game.zip"


def _data(n: int) -> bytes:
    return bytes((i * 7 + 3) % 251 for i in range(n))


def _chunks(data: bytes, sizes: list[int]):
    """Yield ``data`` in chunks of the given (cycling) sizes, so boundaries never align."""
    offset, index = 0, 0
    while offset < len(data):
        step = sizes[index % len(sizes)]
        yield data[offset : offset + step]
        offset += step
        index += 1


def test_blocks_match_hash_blocks_across_odd_chunk_boundaries():
    data = _data(10_000)
    pin = pinbuild.build_pin(URL, _chunks(data, [999, 1, 4097, 3]), block_size=4096)
    assert pin.block_size == 4096
    assert pin.parts[0].url == URL
    assert pin.parts[0].size == len(data)
    assert list(pin.parts[0].blocks) == archivepins.hash_blocks(data, 4096)


def test_exact_multiple_of_block_size_has_no_empty_tail_block():
    data = _data(8192)
    pin = pinbuild.build_pin(URL, _chunks(data, [5000]), block_size=4096)
    assert len(pin.parts[0].blocks) == 2


def test_publisher_md5_is_checked_and_recorded_lowercase():
    data = _data(9000)
    md5 = hashlib.md5(data).hexdigest()
    pin = pinbuild.build_pin(URL, _chunks(data, [1000]), block_size=4096, publisher_md5=md5.upper())
    assert pin.publisher_md5 == md5


def test_md5_mismatch_builds_nothing():
    data = _data(9000)
    with pytest.raises(pinbuild.PinBuildError, match="md5"):
        pinbuild.build_pin(URL, _chunks(data, [1000]), block_size=4096, publisher_md5="0" * 32)


def test_declared_size_must_match_the_stream():
    data = _data(9000)
    with pytest.raises(pinbuild.PinBuildError, match="size"):
        pinbuild.build_pin(URL, _chunks(data[:-1], [1000]), block_size=4096, size=len(data))
    with pytest.raises(pinbuild.PinBuildError, match="size"):
        pinbuild.build_pin(URL, _chunks(data + b"x", [1000]), block_size=4096, size=len(data))


def test_empty_stream_is_an_error():
    with pytest.raises(pinbuild.PinBuildError, match="empty"):
        pinbuild.build_pin(URL, iter(()), block_size=4096)


def test_result_round_trips_through_the_document_format():
    data = _data(9000)
    pin = pinbuild.build_pin(URL, _chunks(data, [777]), block_size=4096)
    parsed = archivepins.parse_document(archivepins.document({"game": pin}))
    assert parsed["game"] == pin


@pytest.mark.parametrize(
    "block_size", [0, 1, 4095, 4097, (16 << 20) + 1, -4096, True, 4096.0, "4096", None]
)
def test_an_invalid_block_size_is_rejected(block_size):
    with pytest.raises(pinbuild.PinBuildError, match="block size"):
        pinbuild.build_pin(URL, iter([b"abc"]), block_size=block_size)


def test_empty_chunks_are_skipped():
    data = _data(9000)
    plain = pinbuild.build_pin(URL, iter([data]), block_size=4096)
    gappy = pinbuild.build_pin(URL, iter([b"", data[:10], b"", data[10:], b""]), block_size=4096)
    assert plain == gappy


def test_no_publisher_md5_leaves_it_unset():
    assert pinbuild.build_pin(URL, iter([_data(100)]), block_size=4096).publisher_md5 is None


def test_a_non_bytes_chunk_is_a_build_error():
    with pytest.raises(pinbuild.PinBuildError, match="bytes"):
        pinbuild.build_pin(URL, iter(["text"]), block_size=4096)


@pytest.mark.parametrize("bad", [12345, "xyz", "0" * 31, "g" * 32])
def test_a_malformed_publisher_md5_is_a_build_error(bad):
    with pytest.raises(pinbuild.PinBuildError, match="md5"):
        pinbuild.build_pin(URL, iter([_data(100)]), block_size=4096, publisher_md5=bad)


def test_a_stream_longer_than_the_declared_size_stops_early():
    sent = []

    def endless():
        while True:
            sent.append(1)
            yield b"x" * 1000

    with pytest.raises(pinbuild.PinBuildError, match="size"):
        pinbuild.build_pin(URL, endless(), block_size=4096, size=5000)
    assert len(sent) <= 7


def test_max_bytes_stops_a_stream_of_unknown_size():
    sent = []

    def endless():
        while True:
            sent.append(1)
            yield b"x" * 1000

    with pytest.raises(pinbuild.PinBuildError, match="limit"):
        pinbuild.build_pin(URL, endless(), block_size=4096, max_bytes=5000)
    assert len(sent) <= 7


def test_the_buffer_stays_bounded_on_a_large_stream():
    import tracemalloc

    block = b"y" * (1 << 20)
    tracemalloc.start()
    try:
        pinbuild.build_pin(URL, (block for _ in range(100)), block_size=1 << 20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 12 << 20


def _counted(limit_chunk: int = 1):
    sent = []

    def gen():
        while True:
            sent.append(1)
            yield b"x" * limit_chunk

    return sent, gen()


def test_the_size_abort_fires_on_the_first_byte_over_the_limit():
    sent, stream = _counted()
    with pytest.raises(pinbuild.PinBuildError, match="size"):
        pinbuild.build_pin(URL, stream, block_size=4096, size=5000)
    assert len(sent) == 5001


def test_a_stream_exactly_the_declared_size_is_accepted():
    data = _data(5000)
    pin = pinbuild.build_pin(URL, iter([data]), block_size=4096, size=5000)
    assert pin.parts[0].size == 5000


def test_max_bytes_stops_at_the_first_byte_over_the_cap():
    sent, stream = _counted()
    with pytest.raises(pinbuild.PinBuildError, match="limit"):
        pinbuild.build_pin(URL, stream, block_size=4096, max_bytes=5000)
    assert len(sent) == 5001


def test_a_stream_exactly_at_max_bytes_is_accepted():
    data = _data(5000)
    pin = pinbuild.build_pin(URL, iter([data]), block_size=4096, max_bytes=5000)
    assert pin.parts[0].size == 5000


def test_a_malformed_md5_says_what_it_expects():
    with pytest.raises(pinbuild.PinBuildError, match="32 hex digits"):
        pinbuild.build_pin(URL, iter([_data(10)]), block_size=4096, publisher_md5="g" * 32)
