# SPDX-License-Identifier: GPL-3.0-or-later
"""#1421: a cut-short Endfield shader cache is replaced by the last good copy.

Seen on an NVIDIA PC 2026-11-15: a 38 s session (it ended with exit 0) left a 1 MB
``vulkan_pso_cache.bin`` over the 189 MB one; the next start rejected it and compiled every
pipeline again. Same GPU, same driver: the header identity matched. A driver update changes that
identity, and then a smaller fresh cache is right.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.direct import DirectBackend, EndfieldBackend
from nadare.core import psoguard
from nadare.core.backends import BackendError

#: Small "megabytes": the tests' temp folder is RAM. The floor scales with it.
MB = 64 * 1024
UUID = bytes(range(16))


@pytest.fixture(autouse=True)
def _small_floor(monkeypatch):
    monkeypatch.setattr(psoguard, "MIN_KEPT_BYTES", 16 * MB)


def _cache_bytes(size: int, *, uuid: bytes = UUID, device: int = 0x2487, fill: int = 7) -> bytes:
    # Unity's own 32-byte header (as in the real file), then Vulkan's VkPipelineCache header.
    unity = struct.pack("<8I", 3, 0x7E5, 3, 0x22, 2, 5, size - 32, 0x9B118180)
    vulkan = struct.pack("<4I", 32, 1, 0x10DE, device) + uuid
    body = bytes([fill]) * (size - 64)
    return unity + vulkan + body


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def where(tmp_path):
    cache = tmp_path / "pfx" / "LocalLow" / "vulkan_pso_cache.bin"
    backup = tmp_path / "data" / "pso-backup"
    return cache, backup


def _guard(where, lines=None):
    cache, backup = where
    return psoguard.Guard(cache, backup, (lines if lines is not None else []).append)


def _session(where, new: bytes | None, lines=None):
    """One Play: before, the game writes ``new`` (or nothing), after."""
    guard = _guard(where, lines)
    guard.before()
    if new is not None:
        _write(where[0], new)
    guard.after()


def _kept(where) -> bytes:
    return (where[1] / where[0].name).read_bytes()


# ---- keeping a copy -------------------------------------------------------------------------
def test_the_first_saved_cache_is_kept(where):
    good = _cache_bytes(20 * MB)
    _session(where, good)
    assert _kept(where) == good


def test_a_grown_cache_replaces_the_kept_copy(where):
    _session(where, _cache_bytes(20 * MB))
    bigger = _cache_bytes(30 * MB, fill=9)
    _session(where, bigger)
    assert _kept(where) == bigger


def test_an_unchanged_cache_is_not_copied_again(where):
    _session(where, _cache_bytes(20 * MB))
    kept = where[1] / where[0].name
    stamp = kept.stat().st_mtime_ns
    os.utime(kept, ns=(stamp - 10**9, stamp - 10**9))
    _session(where, None)  # the game did not write
    assert kept.stat().st_mtime_ns == stamp - 10**9


# ---- putting it back --------------------------------------------------------------------------
def test_a_cut_short_cache_is_replaced_by_the_kept_copy(where):
    good = _cache_bytes(40 * MB)
    _session(where, good)
    lines: list[str] = []
    _session(where, _cache_bytes(1 * MB, fill=1), lines)
    assert where[0].read_bytes() == good
    assert _kept(where) == good
    assert any("cut short" in line for line in lines)


def test_a_driver_update_may_shrink_the_cache(where):
    _session(where, _cache_bytes(40 * MB))
    fresh = _cache_bytes(1 * MB, uuid=bytes(16))  # a new driver: another pipelineCacheUUID
    _session(where, fresh)
    assert where[0].read_bytes() == fresh
    assert _kept(where) == fresh


def test_another_gpu_may_shrink_the_cache(where):
    _session(where, _cache_bytes(40 * MB))
    fresh = _cache_bytes(1 * MB, device=0x1234)
    _session(where, fresh)
    assert where[0].read_bytes() == fresh


def test_a_small_shrink_is_kept(where):
    _session(where, _cache_bytes(40 * MB))
    smaller = _cache_bytes(30 * MB, fill=3)
    _session(where, smaller)
    assert where[0].read_bytes() == smaller
    assert _kept(where) == smaller


def test_a_small_kept_copy_is_never_put_back(where):
    # Below the floor a shrink is ordinary churn, not a lost warm-up.
    _session(where, _cache_bytes(4 * MB))
    tiny = _cache_bytes(1 * MB, fill=2)
    _session(where, tiny)
    assert where[0].read_bytes() == tiny


def test_a_file_of_unknown_shape_is_never_replaced_nor_kept(where):
    good = _cache_bytes(40 * MB)
    _session(where, good)
    odd = b"not a pipeline cache" * 1000
    _session(where, odd)
    assert where[0].read_bytes() == odd
    assert _kept(where) == good  # never judged, never kept
    _session(where, None)  # the next Play leaves an existing file alone
    assert where[0].read_bytes() == odd


def test_a_tiny_file_breaks_nothing(where):
    _session(where, _cache_bytes(40 * MB))
    _session(where, b"tiny")
    assert where[0].read_bytes() == b"tiny"


def test_a_linked_cache_is_never_replaced(where, tmp_path):
    _session(where, _cache_bytes(40 * MB))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(_cache_bytes(1 * MB, fill=1))
    guard = _guard(where)
    guard.before()
    where[0].unlink()
    where[0].symlink_to(outside)
    guard.after()
    assert where[0].is_symlink()
    assert outside.read_bytes() == _cache_bytes(1 * MB, fill=1)


def test_a_missing_cache_is_put_back_at_play(where):
    # A rebuilt prefix (or a deleted file): the warm-up is not lost.
    good = _cache_bytes(20 * MB)
    _session(where, good)
    where[0].unlink()
    lines: list[str] = []
    _guard(where, lines).before()
    assert where[0].read_bytes() == good
    assert lines


def test_nothing_kept_and_nothing_written_does_nothing(where):
    _session(where, None)
    assert not where[0].exists()
    assert not (where[1] / where[0].name).exists()


def test_a_linked_cache_or_copy_is_never_followed(where, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    _write(where[1] / "x", b"")
    (where[1] / where[0].name).symlink_to(outside)
    _session(where, _cache_bytes(20 * MB))
    assert outside.read_bytes() == b"keep"


def test_copies_are_whole_or_absent(where, monkeypatch):
    good = _cache_bytes(20 * MB)
    _session(where, good)

    def broken(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(psoguard, "_copy_into", broken)
    _session(where, _cache_bytes(30 * MB, fill=5))  # no exception reaches the launch
    assert _kept(where) == good
    leftovers = [p.name for p in where[1].iterdir() if p.name != where[0].name]
    assert leftovers == []


def test_a_failed_copy_leaves_no_temp_file(where, monkeypatch):
    good = _cache_bytes(20 * MB)
    _session(where, good)

    def no_sync(_fd):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(psoguard.os, "fsync", no_sync)
    _session(where, _cache_bytes(30 * MB, fill=5))
    assert _kept(where) == good
    assert [p.name for p in where[1].iterdir()] == [where[0].name]


def test_copies_are_synced_before_the_rename(where, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(psoguard.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    _session(where, _cache_bytes(20 * MB))
    assert synced


def test_a_stale_temp_copy_is_removed(where):
    _session(where, _cache_bytes(20 * MB))
    stale = where[1] / f".{where[0].name}.nadare-999999.tmp"
    stale.write_bytes(b"half")
    _session(where, None)
    assert not stale.exists()


# ---- Endfield's Play --------------------------------------------------------------------------
def _endfield(tmp_path):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    return EndfieldBackend(make_context(tmp_path, "direct-endfield", {"prefix": str(prefix)}))


def test_endfield_play_puts_a_cut_short_cache_back(tmp_path, monkeypatch):
    backend = _endfield(tmp_path)
    cache = psoguard.endfield_cache(backend.prefix())
    assert cache.parts[-5:] == (
        "AppData",
        "LocalLow",
        "Gryphline",
        "Endfield",
        "vulkan_pso_cache.bin",
    )
    good = _cache_bytes(40 * MB)
    writes = iter([good, _cache_bytes(1 * MB, fill=1)])

    def play(self):
        self._launch_started = object()  # the game really started
        _write(cache, next(writes))
        return 0

    monkeypatch.setattr(DirectBackend, "launch", play)
    assert backend.launch() == 0
    assert backend.launch() == 0
    assert cache.read_bytes() == good


def test_a_play_that_never_started_the_game_changes_nothing(tmp_path, monkeypatch):
    # Another Nadare's running session may be writing the file: hands off.
    backend = _endfield(tmp_path)
    cache = psoguard.endfield_cache(backend.prefix())
    good = _cache_bytes(40 * MB)

    def play(self):
        self._launch_started = object()
        _write(cache, good)
        return 0

    monkeypatch.setattr(DirectBackend, "launch", play)
    backend.launch()

    def refused(self):
        _write(cache, _cache_bytes(1 * MB, fill=1))  # someone else's session wrote it
        raise BackendError("the game is already running")

    monkeypatch.setattr(DirectBackend, "launch", refused)
    with pytest.raises(BackendError):
        backend.launch()
    assert cache.read_bytes() != good


def test_endfield_play_puts_a_lost_cache_back(tmp_path, monkeypatch):
    backend = _endfield(tmp_path)
    cache = psoguard.endfield_cache(backend.prefix())
    good = _cache_bytes(20 * MB)
    writes = iter([good, None])

    def play(self):
        self._launch_started = object()
        data = next(writes)
        if data is not None:
            _write(cache, data)
        return 0

    monkeypatch.setattr(DirectBackend, "launch", play)
    backend.launch()
    cache.unlink()
    backend.launch()
    assert cache.read_bytes() == good


def test_the_copy_lives_outside_the_prefix(tmp_path, monkeypatch):
    backend = _endfield(tmp_path)
    cache = psoguard.endfield_cache(backend.prefix())

    def play(self):
        self._launch_started = object()
        _write(cache, _cache_bytes(20 * MB))
        return 0

    monkeypatch.setattr(DirectBackend, "launch", play)
    backend.launch()
    kept = backend._data_dir() / "pso-backup" / cache.name
    assert kept.is_file()
    assert backend.prefix() not in kept.parents


def test_nothing_is_put_back_into_a_prefix_not_set_up_yet(where, tmp_path):
    _session(where, _cache_bytes(20 * MB))
    where[0].unlink()
    home = tmp_path / "pfx-new" / "drive_c" / "users" / "steamuser"  # Proton has not run yet
    psoguard.Guard(where[0], where[1], [].append, home=home).before()
    assert not where[0].exists()
    assert not home.exists()
    home.mkdir(parents=True)
    psoguard.Guard(where[0], where[1], [].append, home=home).before()
    assert where[0].exists()
