# SPDX-License-Identifier: GPL-3.0-or-later
"""A worker writes one file per run in its game's folder, and old runs are pruned (issue #838)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import runlog
from nadare.core.config import Config
from nadare.gui import workers


def _worker(tmp_path, monkeypatch, profile="genshin-impact", operation="install"):
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    return workers.BackendWorker(Config.load(tmp_path / "config.json"), profile, operation)


def _run(worker, text="hello"):
    worker._note(text)
    worker._close_log()


def test_a_run_writes_its_own_file_in_the_games_folder(tmp_path, monkeypatch):
    _run(_worker(tmp_path, monkeypatch))
    (path,) = (tmp_path / "logs" / "genshin-impact").glob("install-*.log")
    assert "hello" in path.read_text(encoding="utf-8")


def test_two_runs_get_two_files(tmp_path, monkeypatch):
    for _ in range(2):
        _run(_worker(tmp_path, monkeypatch))
    assert len(list((tmp_path / "logs" / "genshin-impact").glob("install-*.log"))) == 2


def test_a_game_with_spaces_gets_a_safe_folder(tmp_path, monkeypatch):
    _run(_worker(tmp_path, monkeypatch, profile="Blue Archive", operation="launch"))
    folder = tmp_path / "logs" / runlog.game_id("Blue Archive")
    assert len(list(folder.glob("launch-*.log"))) == 1
    assert " " not in folder.name


def test_the_folder_and_file_are_private(tmp_path, monkeypatch):
    old = os.umask(0o022)
    try:
        _run(_worker(tmp_path, monkeypatch))
    finally:
        os.umask(old)
    folder = tmp_path / "logs" / "genshin-impact"
    (path,) = folder.glob("install-*.log")
    assert path.stat().st_mode & 0o777 == 0o600
    assert folder.stat().st_mode & 0o777 == 0o700


def test_starting_a_run_prunes_to_the_newest_five(tmp_path, monkeypatch):
    folder = tmp_path / "logs" / "genshin-impact"
    folder.mkdir(parents=True)
    for i in range(8):
        path = folder / f"install-20260101-12000{i}.log"
        path.write_text(f"run {i}\n", encoding="utf-8")
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    _run(_worker(tmp_path, monkeypatch))
    names = sorted(p.name for p in folder.glob("install-*.log"))
    assert len(names) == runlog.KEEP_RUNS
    assert "install-20260101-120000.log" not in names
    assert "install-20260101-120007.log" in names


def test_the_run_being_written_is_never_pruned(tmp_path, monkeypatch):
    folder = tmp_path / "logs" / "genshin-impact"
    folder.mkdir(parents=True)
    for i in range(8):
        (folder / f"install-20990101-12000{i}.log").write_text("future\n", encoding="utf-8")
    worker = _worker(tmp_path, monkeypatch)
    worker._note("mine")
    worker._close_log()
    mine = [p for p in folder.glob("install-*.log") if "mine" in p.read_text(encoding="utf-8")]
    assert len(mine) == 1


@pytest.mark.parametrize("operation", ["status", "news", "media", "check_update"])
def test_background_work_shares_one_rolling_file(tmp_path, monkeypatch, operation):
    for _ in range(3):
        _run(_worker(tmp_path, monkeypatch, operation=operation), "tick")
    folder = tmp_path / "logs" / "genshin-impact"
    assert [p.name for p in folder.iterdir()] == ["background.log"]
    assert (folder / "background.log").read_text(encoding="utf-8").count("tick") == 3


def test_the_background_file_is_trimmed_when_a_run_opens_it(tmp_path, monkeypatch):
    folder = tmp_path / "logs" / "genshin-impact"
    folder.mkdir(parents=True)
    big = folder / "background.log"
    big.write_text("".join(f"old line {i}\n" for i in range(60_000)), encoding="utf-8")
    assert big.stat().st_size > runlog.BACKGROUND_CAP_BYTES
    _run(_worker(tmp_path, monkeypatch, operation="status"), "fresh")
    assert big.stat().st_size <= runlog.BACKGROUND_CAP_BYTES + 512
    text = big.read_text(encoding="utf-8")
    assert "old line 59999" in text and "old line 0\n" not in text and "fresh" in text


def test_each_run_still_starts_with_its_header(tmp_path, monkeypatch):
    _run(_worker(tmp_path, monkeypatch))
    (path,) = (tmp_path / "logs" / "genshin-impact").glob("install-*.log")
    assert path.read_text(encoding="utf-8").lstrip().startswith("===== ")
