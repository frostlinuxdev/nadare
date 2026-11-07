# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-game log folders: ids, one file per run, and pruning (issue #838)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from nadare.core import runlog

NOW = datetime(2026, 11, 5, 12, 1, 2)


def _make(folder: Path, operation: str, stamp: str, size: int = 10, age: float = 0.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{operation}-{stamp}.log"
    path.write_bytes(b"x" * size)
    mtime = 1_700_000_000 + age
    os.utime(path, (mtime, mtime))
    return path


# ---- ids ---------------------------------------------------------------------------------
def test_a_safe_lowercase_key_is_its_own_id():
    assert runlog.game_id("genshin-impact") == "genshin-impact"
    assert runlog.game_id("pgr_2.1") == "pgr_2.1"


def test_spaces_unicode_and_case_get_a_short_hash_so_they_never_collide():
    a, b = runlog.game_id("Blue Archive"), runlog.game_id("blue-archive")
    assert a != b
    assert a.startswith("blue-archive-") and " " not in a
    assert b == "blue-archive"
    assert runlog.game_id("Genshin") != runlog.game_id("genshin")  # case-insensitive disks


def test_a_key_with_no_safe_characters_still_gets_an_id():
    first, second = runlog.game_id("原神"), runlog.game_id("崩坏")
    assert first.startswith("game-") and second.startswith("game-")
    assert first != second


def test_the_id_is_stable_and_cannot_climb_out_of_the_folder():
    assert runlog.game_id("Blue Archive") == runlog.game_id("Blue Archive")
    for hostile in ("..", ".", "../x", "a/b", ".hidden"):
        gid = runlog.game_id(hostile)
        assert "/" not in gid and gid not in {".", ".."} and not gid.startswith(".")


# ---- paths -------------------------------------------------------------------------------
def test_a_run_gets_its_own_file_in_its_games_folder(tmp_path):
    path = runlog.run_log_path(tmp_path, "genshin-impact", "install", NOW)
    assert path == tmp_path / "genshin-impact" / "install-20261105-120102.log"


def test_two_runs_in_one_second_do_not_share_a_file(tmp_path):
    first = runlog.run_log_path(tmp_path, "g", "install", NOW)
    first.parent.mkdir(parents=True)
    first.write_text("one")
    second = runlog.run_log_path(tmp_path, "g", "install", NOW)
    assert second != first and second.parent == first.parent


def test_background_work_shares_one_rolling_file_per_game(tmp_path):
    assert runlog.background_log_path(tmp_path, "g") == tmp_path / "g" / "background.log"


# ---- pruning -----------------------------------------------------------------------------
def test_only_the_newest_five_runs_of_an_operation_are_kept(tmp_path):
    folder = tmp_path / "g"
    paths = [_make(folder, "install", f"20261105-12000{i}", age=i) for i in range(8)]
    other = _make(folder, "launch", "20261105-120000")
    runlog.prune_game(folder)
    assert [p.exists() for p in paths] == [False, False, False, True, True, True, True, True]
    assert other.exists()


def test_the_size_cap_removes_the_oldest_first_and_keeps_the_newest_run_overall(tmp_path):
    folder = tmp_path / "g"
    old = _make(folder, "install", "20261105-120000", size=600, age=0)
    mid = _make(folder, "launch", "20261105-120001", size=600, age=1)
    new = _make(folder, "update", "20261105-120002", size=600, age=2)
    runlog.prune_game(folder, max_bytes=1000)
    assert not old.exists() and not mid.exists() and new.exists()


def test_a_run_that_is_still_open_is_never_deleted(tmp_path):
    folder = tmp_path / "g"
    open_run = _make(folder, "install", "20261105-120000", size=900, age=0)
    for i in range(1, 8):
        _make(folder, "install", f"20261105-12000{i}", age=i)
    runlog.prune_game(folder, max_bytes=100, protect={open_run})
    assert open_run.exists()


def test_the_newest_run_is_kept_even_when_it_alone_is_over_the_cap(tmp_path):
    folder = tmp_path / "g"
    big = _make(folder, "install", "20261105-120000", size=5000)
    runlog.prune_game(folder, max_bytes=100)
    assert big.exists()


def test_pruning_ignores_files_it_did_not_write(tmp_path):
    folder = tmp_path / "g"
    stray = folder / "notes.txt"
    folder.mkdir()
    stray.write_text("keep me")
    background = folder / "background.log"
    background.write_text("rolling")
    for i in range(8):
        _make(folder, "install", f"20261105-12000{i}", age=i)
    runlog.prune_game(folder, max_bytes=10)
    assert stray.exists() and background.exists()


def test_a_missing_folder_is_not_an_error(tmp_path):
    runlog.prune_game(tmp_path / "nothing")


# ---- the rolling background file ----------------------------------------------------------
def test_the_background_file_keeps_its_tail_on_a_line_boundary(tmp_path):
    path = tmp_path / "background.log"
    path.write_text("".join(f"line {i}\n" for i in range(1000)), encoding="utf-8")
    assert runlog.trim_rolling(path, max_bytes=500) is True
    text = path.read_text(encoding="utf-8")
    assert len(text.encode()) <= 500
    assert text.splitlines()[-1] == "line 999"
    assert text.startswith("line ")  # no half line at the top


def test_a_small_rolling_file_is_left_alone(tmp_path):
    path = tmp_path / "background.log"
    path.write_text("a\nb\n", encoding="utf-8")
    assert runlog.trim_rolling(path, max_bytes=500) is False
