# SPDX-License-Identifier: GPL-3.0-or-later
"""Open folder opens the game's own log folder, and Settings shows where logs are (#1875)."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import paths, runlog
from nadare.gui import logs_dialog
from nadare.gui.logs_dialog import LogsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(
        logs_dialog.QDesktopServices, "openUrl", lambda url: urls.append(url.toLocalFile()) or True
    )
    return urls


def _run(logs, profile, operation="launch", stamp="20261127-100000", mtime=10):
    folder = logs / runlog.game_id(profile)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{operation}-{stamp}.log"
    path.write_text("run\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_a_game_opens_its_own_folder(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    _run(logs, "G")
    _run(logs, "H", mtime=20)
    (logs / "nadare.log").write_text("app\n", encoding="utf-8")
    dlg = LogsDialog(["G", "H"], profile="G", directory=logs, follow=False)
    dlg._open_folder()
    assert opened == [str(logs / runlog.game_id("G"))]


def test_a_key_with_spaces_opens_its_hashed_folder(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    run = _run(logs, "Blue Archive")
    assert run.parent.name != "Blue Archive"  # the slug + hash folder
    dlg = LogsDialog(["Blue Archive"], profile="Blue Archive", directory=logs, follow=False)
    dlg._open_folder()
    assert opened == [str(run.parent)]


def test_a_game_without_runs_yet_opens_the_logs_folder(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    logs.mkdir()
    dlg = LogsDialog(["G"], profile="G", directory=logs, follow=False)
    dlg._open_folder()
    assert opened == [str(logs)]  # never a folder that does not exist


def test_old_flat_log_names_open_the_logs_folder(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "G-launch.log").write_text("old\n", encoding="utf-8")
    dlg = LogsDialog(["G"], profile="G", directory=logs, follow=False)
    dlg._open_folder()
    assert opened == [str(logs)]


def test_without_a_game_the_selected_log_decides(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    (logs).mkdir()
    (logs / "nadare.log").write_text("app\n", encoding="utf-8")
    os.utime(logs / "nadare.log", (5, 5))
    run = _run(logs, "H", mtime=20)
    dlg = LogsDialog(["H"], directory=logs, follow=False)
    assert dlg._selected_path() == run  # newest first
    dlg._open_folder()
    for row in range(dlg.files.count()):
        if dlg.files.item(row).data(logs_dialog.Qt.ItemDataRole.UserRole) == str(
            logs / "nadare.log"
        ):
            dlg.files.setCurrentRow(row)
    dlg._open_folder()
    assert opened == [str(run.parent), str(logs)]


def test_the_folder_button_says_what_it_opens(qapp, tmp_path, opened):
    logs = tmp_path / "logs"
    _run(logs, "G")
    dlg = LogsDialog(["G"], profile="G", directory=logs, follow=False)
    assert str(logs / runlog.game_id("G")) in dlg.folder.toolTip()


def test_settings_show_where_the_game_logs_are(tmp_path, qapp, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = Config.load(tmp_path / "config.json")
    config.profiles["czn"] = {"backend": "generic-wine", "title": "Chaos Zero Nightmare"}
    page = SettingsPage()
    page.load(config, "czn")
    folder = paths.logs_dir() / runlog.game_id("czn")
    assert str(folder) in page.note.text()
    assert str(folder) in page.logs_button.toolTip()
    page.load_app_only(config)
    assert str(paths.logs_dir()) in page.note.text()
