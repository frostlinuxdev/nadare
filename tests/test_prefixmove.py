# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure helper tests for moving a broken Wine prefix aside safely (#8)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from nadare.core import prefixmove


def test_backup_path_uses_a_timestamp_and_never_reuses_one(tmp_path):
    prefix = tmp_path / "prefix"
    now = datetime(2026, 1, 2, 3, 4, 5)
    backup = prefixmove.backup_path(prefix, now=now)
    assert backup == tmp_path / "prefix.broken-20260102-030405"
    backup.mkdir()
    assert prefixmove.backup_path(prefix, now=now) == tmp_path / "prefix.broken-20260102-030405-2"
    (tmp_path / "prefix.broken-20260102-030405-2").mkdir()
    assert prefixmove.backup_path(prefix, now=now) == tmp_path / "prefix.broken-20260102-030405-3"


def test_move_aside_renames_within_the_same_parent(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "drive_c").mkdir()

    backup = prefixmove.move_aside(prefix)

    assert not prefix.exists()
    assert backup.is_dir()
    assert (backup / "drive_c").is_dir()
    assert backup.parent == prefix.parent


def test_move_aside_refuses_missing_and_symlinked_prefixes(tmp_path):
    with pytest.raises(prefixmove.PrefixMoveError):
        prefixmove.move_aside(tmp_path / "missing")

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(prefixmove.PrefixMoveError):
        prefixmove.move_aside(link)
    assert link.is_symlink()
    assert real.is_dir()


def test_move_aside_refuses_root_home_and_home_ancestors(tmp_path):
    for dangerous in (Path("/"), Path.home(), Path.home().parent):
        with pytest.raises(prefixmove.PrefixMoveError):
            prefixmove.move_aside(dangerous)


def test_restore_inside_moves_only_paths_inside_and_never_overwrites(tmp_path):
    prefix = tmp_path / "prefix"
    backup = tmp_path / "prefix.broken-20260102-030405"
    inside = prefix / "drive_c" / "game" / "a.txt"
    already = prefix / "drive_c" / "game" / "keep.txt"
    outside = tmp_path / "outside" / "b.txt"

    (backup / "drive_c" / "game").mkdir(parents=True)
    (backup / "drive_c" / "game" / "a.txt").write_text("new")
    (backup / "drive_c" / "game" / "keep.txt").write_text("old")
    (backup / "drive_c" / "game" / "missing-in-prefix.txt").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "b.txt").write_text("outside")
    prefix.mkdir()
    already.parent.mkdir(parents=True)
    already.write_text("newer")

    moved = prefixmove.restore_inside(
        backup,
        prefix,
        [inside, already, outside, prefix / "drive_c" / "game" / "missing.txt"],
    )

    assert moved == [inside]
    assert inside.read_text() == "new"
    assert already.read_text() == "newer", "an existing destination is never overwritten"
    assert outside.read_text() == "outside"


def test_roll_back_restores_the_old_prefix_and_parks_a_half_built_one(tmp_path):
    prefix = tmp_path / "prefix"
    backup = tmp_path / "prefix.broken-20260102-030405"
    backup.mkdir()
    (backup / "old.txt").write_text("old")

    prefixmove.roll_back(backup, prefix)
    assert (prefix / "old.txt").read_text() == "old"
    assert not backup.exists()

    backup.mkdir()
    (backup / "old2.txt").write_text("old2")
    prefix.mkdir(exist_ok=True)
    (prefix / "half.txt").write_text("half")

    prefixmove.roll_back(backup, prefix)

    assert (prefix / "old2.txt").read_text() == "old2"
    assert not backup.exists()
    parked = list(tmp_path.glob("prefix.failed-*"))
    assert len(parked) == 1
    assert (parked[0] / "half.txt").read_text() == "half"


def test_roll_back_moves_restored_paths_back_into_the_old_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    backup = tmp_path / "prefix.broken-20260102-030405"
    (backup / "drive_c").mkdir(parents=True)
    (backup / "drive_c" / "stale.dll").write_text("old")
    # What a half-finished rebuild looks like: a new prefix holding the moved game dir.
    (prefix / "drive_c" / "Game").mkdir(parents=True)
    (prefix / "drive_c" / "Game" / "game.exe").write_text("game")
    moved = [prefix / "drive_c" / "Game"]

    prefixmove.roll_back(backup, prefix, moved)

    assert (prefix / "drive_c" / "Game" / "game.exe").read_text() == "game"
    assert (prefix / "drive_c" / "stale.dll").read_text() == "old"
    parked = list(tmp_path.glob("prefix.failed-*"))
    assert len(parked) == 1
    assert not (parked[0] / "drive_c" / "Game" / "game.exe").exists(), (
        "the user's game files must not be left in the parked half-built prefix"
    )
