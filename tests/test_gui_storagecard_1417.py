# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1417 / #1617: the Disk space card's new rows and their Clear buttons.

The card shows installers, each game's prefix ``Temp``, old Steam prefix backups and
the shader caches #1364 owns.  A running game greys its ``Temp`` row, the newest
backup of an app id is kept until the player picks it, and every clear asks once
with the size.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import paths, shadercache, storagecard
from nadare.core.config import Config
from nadare.gui import disk_space_panel
from nadare.gui.disk_space_panel import DiskSpacePanel

MIB = 1024 * 1024
DAY = 86400
WV2 = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _file(path: Path, size: int = 10, age: float = 2 * DAY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def _dir(path: Path, files: int = 1, size: int = 10) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        _file(path / f"f{i}", size)
    return path


def _temp(prefix: Path, size: int = MIB) -> Path:
    folder = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Temp"
    return _dir(folder, files=1, size=size)


def _backup(compatdata: Path, name: str, size: int = 50, age: float = DAY) -> Path:
    return _dir(compatdata / name, files=1, size=size)


def _wait(qapp, predicate, seconds: float = 15.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _config(tmp_path: Path, *profiles: tuple[str, dict]) -> Config:
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles:
        config.add_profile(name, **values)
    return config


def _panel(tmp_path: Path, *profiles: tuple[str, dict]) -> DiskSpacePanel:
    panel = DiskSpacePanel()
    panel.load(_config(tmp_path, *profiles))
    panel.refresh()
    return panel


def test_the_card_lists_all_four_categories(qapp, tmp_path, monkeypatch):
    _file(paths.cache_dir() / "czn" / WV2, size=30)
    prefix = tmp_path / "prefix"
    _temp(prefix, 4 * MIB)
    _backup(tmp_path / "steamapps" / "compatdata", "5019180.bak-pre-webview2", 6 * MIB)
    _dir(shadercache.mesa_dir(), files=1, size=8 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [tmp_path / "steamapps"])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        assert "30 B" in panel.installers_label.text()
        assert panel.installers_button.isEnabled()
        assert len(panel.temp_rows) == 1
        assert "Game" in panel.temp_rows[0].label.text()
        assert panel.temp_rows[0].button.isEnabled()
        assert len(panel.backup_rows) == 1
        assert "Epic Seven" in panel.backup_rows[0].label.text()
        assert len(panel.shader_rows) >= 1
        assert any("Mesa" in row.label.text() for row in panel.shader_rows)
    finally:
        panel.deleteLater()


def test_clear_installers_asks_once_with_the_size_and_deletes_only_them(
    qapp, tmp_path, monkeypatch
):
    installer = _file(paths.cache_dir() / "czn" / WV2, size=30)
    prefix = tmp_path / "prefix"
    temp = _temp(prefix, 4 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    asked: list[str] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_ask", lambda self, title, text: asked.append(text) or True
    )
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.installers_button.click()
        assert _wait(qapp, lambda: panel.cleared_once)
        assert len(asked) == 1 and "30 B" in asked[0]
        assert not installer.exists()
        assert temp.exists()
        assert "Deleted" in panel.status.text() or "deleted" in panel.status.text()
    finally:
        panel.deleteLater()


def test_no_keeps_every_installer(qapp, tmp_path, monkeypatch):
    class T:
        def __init__(self, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(disk_space_panel.threading, "Thread", T)
    installer = _file(paths.cache_dir() / "czn" / WV2, size=30)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    monkeypatch.setattr(DiskSpacePanel, "_ask", lambda self, title, text: False)
    panel = _panel(tmp_path)
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.installers_button.click()
        qapp.processEvents()
        assert not panel.cleared_once
        assert installer.exists()
    finally:
        panel.deleteLater()


def test_a_running_game_greys_its_temp_row(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _temp(prefix)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: True)
    panel = _panel(tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)}))
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        assert len(panel.temp_rows) == 1
        assert not panel.temp_rows[0].button.isEnabled()
        assert "running" in panel.temp_rows[0].label.text().lower()
    finally:
        panel.deleteLater()


def test_the_newest_backup_is_kept_until_picked(qapp, tmp_path, monkeypatch):
    compatdata = tmp_path / "steamapps" / "compatdata"
    _backup(compatdata, "5019180.bak-pre-webview2", 10 * MIB, age=2 * DAY)
    _backup(compatdata, "5019180.bak-pre-webview2-2", 20 * MIB, age=DAY)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [tmp_path / "steamapps"])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    panel = _panel(tmp_path)
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        assert len(panel.backup_rows) == 2
        old, new = panel.backup_rows
        assert old.button.isEnabled()
        # Opt-in now lives in the confirmation, so both rows can open it.
        assert new.button.isEnabled()
        assert not new.findChildren(disk_space_panel.QCheckBox)
    finally:
        panel.deleteLater()


def test_clearing_a_backup_deletes_it_after_the_yes(qapp, tmp_path, monkeypatch):
    compatdata = tmp_path / "steamapps" / "compatdata"
    old = _backup(compatdata, "5019180.bak-pre-webview2", 10 * MIB, age=2 * DAY)
    new = _backup(compatdata, "5019180.bak-pre-webview2-2", 20 * MIB, age=DAY)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [tmp_path / "steamapps"])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    asked: list[str] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_ask", lambda self, title, text: asked.append(text) or True
    )
    panel = _panel(tmp_path)
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.backup_rows[0].button.click()
        assert _wait(qapp, lambda: panel.cleared_once)
        assert len(asked) == 1 and "10 MiB" in asked[0]
        assert not old.exists() and new.exists()
    finally:
        panel.deleteLater()


def test_clear_temp_asks_with_the_size_and_deletes_it(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    temp = _temp(prefix, 4 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    asked: list[str] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_ask", lambda self, title, text: asked.append(text) or True
    )
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.temp_rows[0].button.click()
        assert _wait(qapp, lambda: panel.cleared_once)
        assert len(asked) == 1 and "4 MiB" in asked[0]
        assert temp.is_dir() and not list(temp.iterdir())
    finally:
        panel.deleteLater()


def test_clear_shader_asks_with_the_size_and_deletes_it(qapp, tmp_path, monkeypatch):
    mesa = _dir(shadercache.mesa_dir(), files=1, size=8 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    asked: list[str] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_ask", lambda self, title, text: asked.append(text) or True
    )
    panel = _panel(tmp_path)
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        row = next(row for row in panel.shader_rows if "Mesa" in row.label.text())
        row.button.click()
        assert _wait(qapp, lambda: panel.cleared_once)
        assert len(asked) == 1 and "8 MiB" in asked[0]
        assert not mesa.exists()
    finally:
        panel.deleteLater()


def test_a_recovered_settings_file_checks_no_new_category(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)

    class T:
        def __init__(self, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(disk_space_panel.threading, "Thread", T)
    config = _config(tmp_path)
    config.notice = "config was damaged and recovered"
    panel = DiskSpacePanel()
    try:
        panel.load(config)
        panel.refresh()
        qapp.processEvents()
        assert panel.report is None
        assert not panel.installers_button.isEnabled()
        assert panel.temp_rows == [] and panel.backup_rows == [] and panel.shader_rows == []
    finally:
        panel.deleteLater()


def test_review_refresh_during_clear_keeps_buttons_disabled(qapp, tmp_path, monkeypatch):
    jobs = []

    class DeferredThread:
        def __init__(self, target, args=(), **_kwargs):
            jobs.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(disk_space_panel.threading, "Thread", DeferredThread)
    panel = DiskSpacePanel()
    panel.load(_config(tmp_path))
    report = storagecard.Report(installers=storagecard.Installers((tmp_path / "x.exe",), 7))
    panel._fill_report(report)
    panel.report = report
    try:
        panel._start_clear(lambda: storagecard.ClearResult())
        panel.refresh()
        panel._on_measured(panel._generation, [], [], report)
        assert not panel.installers_button.isEnabled()
        count = len(jobs)
        panel._start_clear(lambda: storagecard.ClearResult())
        assert len(jobs) == count, "a second deletion started during the first"
    finally:
        panel.deleteLater()


def test_review_clear_error_is_visible(qapp, monkeypatch):
    panel = DiskSpacePanel()
    monkeypatch.setattr(panel, "refresh", lambda: None)
    try:
        panel._on_cleared(storagecard.ClearResult(reason="permission denied"))
        assert "permission denied" in panel.status.text()
    finally:
        panel.deleteLater()


def test_review_unknown_shader_size_is_left_alone(qapp, tmp_path, monkeypatch):
    panel = DiskSpacePanel()
    asked = []
    entry = storagecard.ShaderCache("mesa", "Mesa", None, (tmp_path / "mesa",), None, False)
    monkeypatch.setattr(panel, "_ask", lambda title, text: asked.append(text) or False)
    try:
        panel._fill_report(storagecard.Report(shaders=(entry,)))
        # An unmeasured entry cannot meet the 1 MiB threshold; leave it alone.
        assert not panel.shader_rows
        assert not panel.categories["shader"].button.isEnabled()
        assert "Nothing to clear" in panel.categories["shader"].label.text()
        assert not asked
    finally:
        panel.deleteLater()
