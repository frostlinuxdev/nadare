# SPDX-License-Identifier: GPL-3.0-or-later
"""A Move to another disk checks contents, not only sizes (#1366 A10; DECISIONS 2026-11-15).

Only temporary synthetic files are moved or deleted.
"""

from __future__ import annotations

import os

import pytest

from nadare.core import relocate


def _game(root):
    root.mkdir()
    (root / "save.dat").write_bytes(b"old-save")
    (root / "sub").mkdir()
    (root / "sub" / "data.bin").write_bytes(b"x" * 300_000)
    return root


@pytest.fixture
def other_disk(monkeypatch):
    monkeypatch.setattr(relocate, "_same_device", lambda *args: False)


def test_original_is_kept_when_it_was_written_after_the_copy(tmp_path, other_disk):
    """A same-size write to the original after copying would be lost with it."""
    source, destination = _game(tmp_path / "source"), tmp_path / "destination"
    relocate.move_tree(source, destination)
    (source / "save.dat").write_bytes(b"new-save")  # same length, written now
    result = relocate.remove_original(source, destination)
    assert not result.removed
    assert (source / "save.dat").read_bytes() == b"new-save"


def test_writes_at_the_new_place_do_not_keep_the_original(tmp_path, other_disk):
    """The game already runs from the copy (Wine rewrites system.reg): the original is stale."""
    source, destination = _game(tmp_path / "source"), tmp_path / "destination"
    relocate.move_tree(source, destination)
    (destination / "save.dat").write_bytes(b"new-save")
    assert relocate.remove_original(source, destination).removed
    assert (destination / "save.dat").read_bytes() == b"new-save"


def test_an_unchanged_copy_lets_the_original_go(tmp_path, other_disk):
    source, destination = _game(tmp_path / "source"), tmp_path / "destination"
    relocate.move_tree(source, destination)
    assert relocate.remove_original(source, destination).removed
    assert not source.exists()
    assert (destination / "save.dat").read_bytes() == b"old-save"


def test_coarse_timestamps_on_the_new_disk_still_match(tmp_path, other_disk):
    """exFAT/FAT keep 2 s steps: a copy whose times were rounded is still the same game."""
    source, destination = _game(tmp_path / "source"), tmp_path / "destination"
    relocate.move_tree(source, destination)
    for path in (destination / "save.dat", destination / "sub" / "data.bin"):
        info = os.stat(path)
        rounded = info.st_mtime_ns - info.st_mtime_ns % 2_000_000_000  # FAT: 2 s steps, down
        os.utime(path, ns=(info.st_atime_ns, rounded))
    assert relocate.remove_original(source, destination).removed


def test_a_copy_corrupted_while_copying_is_refused_and_the_original_stays(
    tmp_path, other_disk, monkeypatch
):
    source, destination = _game(tmp_path / "source"), tmp_path / "destination"
    real_copy = relocate._copy

    def corrupting_copy(src, partial, *args, **kwargs):
        out = real_copy(src, partial, *args, **kwargs)
        victim = partial / "sub" / "data.bin"
        info = os.stat(victim)
        data = bytearray(victim.read_bytes())
        data[1234] ^= 0xFF  # same size, same timestamps: only the bytes differ
        victim.write_bytes(bytes(data))
        os.utime(victim, ns=(info.st_atime_ns, info.st_mtime_ns))
        return out

    monkeypatch.setattr(relocate, "_copy", corrupting_copy)
    with pytest.raises(relocate.RelocateError, match="verified"):
        relocate.move_tree(source, destination)
    assert (source / "sub" / "data.bin").read_bytes() == b"x" * 300_000
    assert not destination.exists()
    assert not destination.with_name(destination.name + relocate.PARTIAL_SUFFIX).exists()
