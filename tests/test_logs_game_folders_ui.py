# SPDX-License-Identifier: GPL-3.0-or-later
"""The zip bundle and the Logs dialog work with per-game run folders (issue #838)."""

from __future__ import annotations

import os
import zipfile

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import logview, runlog
from nadare.gui.logs_dialog import LogsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _run(logs, profile, operation, stamp, text, mtime):
    folder = logs / runlog.game_id(profile)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{operation}-{stamp}.log"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_the_bundle_keeps_each_run_under_its_game_folder(tmp_path):
    logs = tmp_path / "logs"
    _run(logs, "G", "launch", "20261105-100000", "one", 10)
    _run(logs, "H", "launch", "20261105-100000", "two", 20)  # same file name, other game
    (logs / "nadare.log").write_text("app", encoding="utf-8")
    target = tmp_path / "bundle.zip"
    assert logview.bundle(["G", "H"], logs, target) == 3
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
    assert f"{runlog.game_id('G')}/launch-20261105-100000.log" in names
    assert f"{runlog.game_id('H')}/launch-20261105-100000.log" in names
    assert "nadare.log" in names and "README.txt" in names


def test_the_bundle_still_flattens_old_log_names(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "G-launch.log").write_text("old", encoding="utf-8")
    target = tmp_path / "bundle.zip"
    logview.bundle(["G"], logs, target)
    with zipfile.ZipFile(target) as zf:
        assert "G-launch.log" in zf.namelist()


def test_following_a_run_switches_to_the_next_run_when_it_starts(qapp, tmp_path):
    logs = tmp_path / "logs"
    first = _run(logs, "G", "install", "20261105-100000", "first run\n", 10)
    dlg = LogsDialog(["G"], profile="G", directory=logs, follow=False)
    assert dlg._selected_path() == first
    second = _run(logs, "G", "install", "20261105-110000", "second run\n", 20)
    dlg._tick()
    assert dlg._selected_path() == second
    assert "second run" in dlg.view.toPlainText()


def test_a_run_of_another_operation_does_not_steal_the_selection(qapp, tmp_path):
    logs = tmp_path / "logs"
    install = _run(logs, "G", "install", "20261105-100000", "installing\n", 10)
    dlg = LogsDialog(["G"], profile="G", directory=logs, follow=False, operation="install")
    _run(logs, "G", "launch", "20261105-110000", "launching\n", 20)
    dlg._tick()
    assert dlg._selected_path() == install
