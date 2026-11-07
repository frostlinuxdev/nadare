# SPDX-License-Identifier: GPL-3.0-or-later
"""The support report reads a bounded tail of a log, never the whole file (#858)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import report


class _Counting:
    """Wraps a real file object and counts the bytes read from it."""

    total = 0

    def __init__(self, handle):
        self._handle = handle

    def read(self, size=-1):
        data = self._handle.read(size)
        _Counting.total += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


@pytest.fixture
def counted(monkeypatch):
    real_open = Path.open
    _Counting.total = 0

    def counting_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return _Counting(handle) if "b" in mode else handle

    monkeypatch.setattr(Path, "open", counting_open)
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("read_bytes"))
    )
    return _Counting


def test_a_huge_log_is_read_only_at_its_end(tmp_path, counted):
    log = tmp_path / "install-1.log"
    log.write_text("".join(f"line {i}\n" for i in range(800_000)))  # about 7 MB
    text = report._tail(log)
    assert text.splitlines()[-1] == "line 799999"
    assert len(text.splitlines()) == 200
    assert counted.total <= report._TAIL_BYTES + 64 * 1024


def test_the_newest_whole_lines_are_kept(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("".join(f"row {i}\n" for i in range(500)))
    lines = report._tail(log, lines=3).splitlines()
    assert lines == ["row 497", "row 498", "row 499"]


def test_a_cut_in_the_middle_of_a_line_drops_that_partial_line(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "_TAIL_BYTES", 40)
    log = tmp_path / "a.log"
    log.write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nlast one\nvery last\n")
    assert report._tail(log) == "last one\nvery last"


def test_a_short_log_is_returned_whole(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("one\ntwo\n")
    assert report._tail(log) == "one\ntwo"


def test_secrets_are_still_masked(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("token=abcdef123456789012345678901234567890 and more\n")
    assert "abcdef123456789012345678901234567890" not in report._tail(log)


def test_an_empty_or_missing_log_is_reported_plainly(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("")
    assert report._tail(empty) == "(empty)"
    assert report._tail(tmp_path / "missing.log").startswith("unavailable:")
