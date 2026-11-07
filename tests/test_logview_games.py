# SPDX-License-Identifier: GPL-3.0-or-later
"""The log list reads per-game folders next to the old flat files (issue #838)."""

from __future__ import annotations

import os
from pathlib import Path

from nadare.core import logview, runlog


def _write(path: Path, text: str = "x\n", mtime: float = 1_700_000_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_run_files_in_a_game_folder_are_listed_under_their_game(tmp_path):
    _write(tmp_path / "genshin-impact" / "install-20261105-120102.log", mtime=10)
    _write(tmp_path / "genshin-impact" / "launch-20261105-130000.log", mtime=20)
    found = logview.list_logs(["genshin-impact"], tmp_path)
    assert [(f.profile, f.operation) for f in found] == [
        ("genshin-impact", "launch"),
        ("genshin-impact", "install"),
    ]
    assert found[0].label == "genshin-impact · launch"


def test_a_game_with_spaces_in_its_key_is_found_by_its_folder_id(tmp_path):
    folder = runlog.game_id("Blue Archive")
    _write(tmp_path / folder / "launch-20261105-120102.log")
    found = logview.list_logs(["Blue Archive", "other"], tmp_path, profile="Blue Archive")
    assert [(f.profile, f.operation) for f in found] == [("Blue Archive", "launch")]
    assert logview.list_logs(["Blue Archive", "other"], tmp_path, profile="other") == []


def test_the_background_file_is_listed_as_background(tmp_path):
    _write(tmp_path / "g" / "background.log")
    (found,) = logview.list_logs(["g"], tmp_path)
    assert (found.profile, found.operation) == ("g", "background")


def test_old_flat_files_are_still_listed_beside_the_folders(tmp_path):
    _write(tmp_path / "G-launch.log", mtime=5)
    _write(tmp_path / "g" / "install-20261105-120102.log", mtime=9)
    _write(tmp_path / "nadare.log", mtime=1)
    found = logview.list_logs(["G", "g"], tmp_path)
    assert {(f.profile, f.operation) for f in found} == {
        ("G", "launch"),
        ("g", "install"),
        (None, "app"),
    }


def test_a_folder_of_a_removed_game_is_still_shown_under_its_id(tmp_path):
    _write(tmp_path / "gone-game" / "install-20261105-120102.log")
    (found,) = logview.list_logs(["something-else"], tmp_path)
    assert (found.profile, found.operation) == ("gone-game", "install")


def test_files_that_are_not_ours_inside_a_folder_are_ignored(tmp_path):
    _write(tmp_path / "g" / "notes.txt")
    _write(tmp_path / "g" / "install-20261105-120102.log.tmp")
    assert logview.list_logs(["g"], tmp_path) == []


def test_run_name_parsing():
    assert runlog.operation_of(Path("install-20261105-120102.log")) == "install"
    assert runlog.operation_of(Path("update-20261105-120102-3.log")) == "update"
    assert runlog.operation_of(Path("background.log")) == "background"
    assert runlog.operation_of(Path("notes.txt")) is None
