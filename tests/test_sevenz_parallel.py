# SPDX-License-Identifier: GPL-3.0-or-later
"""Parallel extraction of a 7z with many independent solid blocks (nadare.core.sevenz)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nadare.core import sevenz
from nadare.core.events import Cancel, Cancelled

FIX = Path(__file__).parent / "fixtures" / "sevenz"
ARCHIVE = FIX / "multiblock.7z"


def _blob(n: int, k: int) -> bytes:
    return bytes(((i * 31 + (i >> 7) + k) & 0xFF) for i in range(n))


# What multiblock.7z holds: 24 files in 4 dirs (13 solid blocks of about 2 files) + an empty file.
CONTENT = {f"dir{i % 4}/f{i:02d}.bin": _blob(60_000 + i * 977, i) for i in range(24)}
CONTENT["empty.dat"] = b""


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class Opener:
    """An ``open_reader`` that records how it is used (thread-safe)."""

    def __init__(self, path: Path = ARCHIVE, delay: float = 0.0):
        self.path, self.delay = path, delay
        self.lock = threading.Lock()
        self.opened: list = []
        self.open_now = 0
        self.max_open = 0

    def __call__(self):
        fh = open(self.path, "rb")
        real_close = fh.close
        outer = self

        class Tracked:
            def read(self, *a):
                if outer.delay:
                    time.sleep(outer.delay)
                return fh.read(*a)

            def seek(self, *a):
                return fh.seek(*a)

            def tell(self):
                return fh.tell()

            def close(self):
                with outer.lock:
                    outer.open_now -= 1
                real_close()

            def __getattr__(self, name):
                return getattr(fh, name)

        tracked = Tracked()
        with self.lock:
            self.opened.append(tracked)
            self.open_now += 1
            self.max_open = max(self.max_open, self.open_now)
        return tracked


def test_the_fixture_really_has_many_solid_blocks():
    with open(ARCHIVE, "rb") as fh:
        assert len(sevenz._parse_archive(fh).folders) >= 8


def test_sequential_extraction_of_the_fixture_is_correct(tmp_path):
    with open(ARCHIVE, "rb") as fh:
        assert sevenz.extract(fh, tmp_path) == len(CONTENT)
    assert tree(tmp_path) == CONTENT


@pytest.mark.parametrize("workers", [2, 4, 8])
def test_parallel_extraction_matches_the_original_tree(tmp_path, workers):
    opener = Opener()
    with open(ARCHIVE, "rb") as fh:
        n = sevenz.extract(fh, tmp_path, workers=workers, open_reader=opener)
    assert tree(tmp_path) == CONTENT
    assert n == len(CONTENT)
    assert opener.open_now == 0, "every worker reader must be closed"


def test_parallel_extraction_really_runs_blocks_at_the_same_time(tmp_path):
    opener = Opener(delay=0.002)  # slow reads so the workers overlap
    with open(ARCHIVE, "rb") as fh:
        sevenz.extract(fh, tmp_path, workers=4, open_reader=opener)
    assert opener.max_open >= 2
    assert 2 <= len(opener.opened) <= 4, "one reader per worker, not one per block"
    assert len({id(r) for r in opener.opened}) == len(opener.opened)


def test_parallel_progress_is_serialised_monotonic_and_complete(tmp_path):
    calls: list[tuple[int, int]] = []
    inside = threading.Lock()
    overlap = []

    def progress(done, total):
        if not inside.acquire(blocking=False):
            overlap.append(1)  # two threads in the callback at once
            return
        try:
            time.sleep(0.0005)
            calls.append((done, total))
        finally:
            inside.release()

    with open(ARCHIVE, "rb") as fh:
        sevenz.extract(fh, tmp_path, workers=4, open_reader=Opener(), progress=progress)
    assert not overlap, "progress must never be called concurrently"
    dones = [d for d, _ in calls]
    assert dones == sorted(dones), "progress must not go backwards"
    assert calls[-1][0] == calls[-1][1] == sum(len(v) for v in CONTENT.values())
    assert len({t for _, t in calls}) == 1


def test_parallel_cancel_stops_all_workers(tmp_path):
    cancel = Cancel()
    seen = []

    def progress(done, total):
        seen.append(done)
        cancel.cancel()

    opener = Opener(delay=0.001)
    with open(ARCHIVE, "rb") as fh, pytest.raises(Cancelled):
        sevenz.extract(
            fh, tmp_path, workers=4, open_reader=opener, cancel=cancel, progress=progress
        )
    assert opener.open_now == 0
    assert not [t for t in threading.enumerate() if t.name.startswith("ThreadPoolExecutor")]
    for name, data in tree(tmp_path).items():
        assert data == CONTENT[name], f"a partly written file was left behind: {name}"


def test_a_damaged_block_fails_the_whole_parallel_extraction_and_leaves_no_bad_file(tmp_path):
    raw = bytearray(ARCHIVE.read_bytes())
    raw[32 + len(raw) // 3] ^= 0xFF  # inside the packed data of some block
    bad = tmp_path / "bad.7z"
    bad.write_bytes(bytes(raw))
    out = tmp_path / "out"
    with open(bad, "rb") as fh, pytest.raises(sevenz.SevenZipError):
        sevenz.extract(fh, out, workers=4, open_reader=Opener(bad))
    for name, data in tree(out).items():
        assert data == CONTENT[name], f"a corrupt file was left behind: {name}"


def test_only_filter_opens_readers_only_for_blocks_that_are_needed(tmp_path):
    opener = Opener()
    wanted = {name for name in CONTENT if name.startswith("dir1/")}
    with open(ARCHIVE, "rb") as fh:
        sevenz.extract(fh, tmp_path, only=wanted.__contains__, workers=8, open_reader=opener)
    assert tree(tmp_path) == {k: CONTENT[k] for k in wanted}
    assert len(opener.opened) <= len(wanted)


def test_one_worker_or_no_factory_stays_sequential(tmp_path):
    def forbidden():
        pytest.fail("no extra reader may be opened")

    with open(ARCHIVE, "rb") as fh:
        sevenz.extract(fh, tmp_path / "a", workers=1, open_reader=forbidden)
        fh.seek(0)
        sevenz.extract(fh, tmp_path / "b", workers=4)  # workers without a factory: sequential
    assert tree(tmp_path / "a") == CONTENT
    assert tree(tmp_path / "b") == CONTENT


def test_a_reader_that_cannot_be_opened_is_reported(tmp_path):
    def broken():
        raise OSError("connection refused")

    with open(ARCHIVE, "rb") as fh, pytest.raises(OSError, match="refused"):
        sevenz.extract(fh, tmp_path, workers=4, open_reader=broken)
