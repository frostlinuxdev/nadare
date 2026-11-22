# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate 5: reject hostile full-install headers before reading their payload."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from nadare.core import hdiffsf, krdiff

FIXTURE = Path(__file__).parent / "fixtures" / "hdiff" / "full_small.hdiff"


def _varint(value: int) -> bytes:
    groups = [value & 0x7F]
    while value >> 7:
        value >>= 7
        groups.append(value & 0x7F)
    return bytes(
        group | (0x80 if index else 0) for index, group in reversed(list(enumerate(groups)))
    )


def _body_field(index: int, value: int) -> io.BytesIO:
    data = FIXTURE.read_bytes()
    stream = io.BytesIO(data)
    krdiff.read_dir_head(stream, allow_old=False)
    assert krdiff._delim(stream, ord("&"), 10) == "HDIFFSF20"
    krdiff._delim(stream, 0, 10)
    for _ in range(index):
        krdiff._varint(stream)
    start = stream.tell()
    krdiff._varint(stream)
    end = stream.tell()
    return io.BytesIO(data[:start] + _varint(value) + data[end:])


def test_body_size_must_match_the_directory_file_list():
    header = hdiffsf.read_header(io.BytesIO(FIXTURE.read_bytes()))
    with pytest.raises(hdiffsf.HDiffError, match="does not match its file list"):
        hdiffsf.read_header(_body_field(0, header.new_size + 1))


def test_step_buffer_cannot_exceed_the_declared_data_budget():
    header = hdiffsf.read_header(io.BytesIO(FIXTURE.read_bytes()))
    # The size-relative bound is stricter than the absolute 64 MiB ceiling here.
    limit = min(header.new_size, header.raw_size) + 1024 * 1024
    assert hdiffsf.read_header(_body_field(3, limit)).step_mem == limit
    with pytest.raises(hdiffsf.HDiffError, match="step buffer is larger than allowed"):
        hdiffsf.read_header(_body_field(3, limit + 1))
