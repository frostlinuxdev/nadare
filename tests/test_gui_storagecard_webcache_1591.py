# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1591 item 1: the launcher web cache rows on the Disk space card.

Each game with an embedded browser profile gets one row with its cache size and
its own Clear.  Clear asks once with the size, runs on the card's worker thread,
and a running game's row is greyed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import storagecard
from nadare.core.config import Config
from nadare.gui.disk_space_panel import DiskSpacePanel

MIB = 1024 * 1024
DAY = 86400


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


def _web_profile(prefix: Path, size: int = 20 * MIB) -> Path:
    profile = prefix / "drive_c/users/steamuser/AppData/Local/CEF/User Data"
    _dir(profile / "Cache", files=1, size=size)
    _file(profile / "Cookies", size=0)
    return profile


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


def test_the_card_lists_a_launcher_web_cache_with_its_size(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _web_profile(prefix, 20 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        assert len(panel.web_rows) == 1
        text = panel.web_rows[0].label.text()
        assert "Game" in text and "20 MiB" in text
        assert panel.web_rows[0].button.isEnabled()
    finally:
        panel.deleteLater()


def test_clear_web_cache_asks_once_with_the_size_and_deletes_it(
    qapp, tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix"
    profile = _web_profile(prefix, 20 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    asked: list[str] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_ask", lambda self, title, text: asked.append(text) or True
    )
    worker_threads: list[str] = []
    real_clear = storagecard.clear_webcache

    def spy(entry):
        worker_threads.append(threading.current_thread().name)
        return real_clear(entry)

    monkeypatch.setattr(storagecard, "clear_webcache", spy)
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.web_rows[0].button.click()
        assert _wait(qapp, lambda: panel.cleared_once)
        assert len(asked) == 1 and "20 MiB" in asked[0]
        assert worker_threads == ["disk-space-clear"]
        assert profile.joinpath("Cache").is_dir()
        assert list(profile.joinpath("Cache").iterdir()) == []
        assert (profile / "Cookies").exists()
    finally:
        panel.deleteLater()


def test_a_running_game_greys_its_web_cache_row(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _web_profile(prefix)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: True)
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        assert len(panel.web_rows) == 1
        assert not panel.web_rows[0].button.isEnabled()
        assert "running" in panel.web_rows[0].label.text().lower()
    finally:
        panel.deleteLater()


def test_no_keeps_the_launcher_web_cache(qapp, tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    profile = _web_profile(prefix, 20 * MIB)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])
    monkeypatch.setattr(storagecard, "is_running", lambda _prefix: False)
    monkeypatch.setattr(DiskSpacePanel, "_ask", lambda self, title, text: False)
    started: list[object] = []
    monkeypatch.setattr(
        DiskSpacePanel, "_start_clear", lambda self, work: started.append(work)
    )
    panel = _panel(
        tmp_path, ("g", {"backend": "wine", "title": "Game", "prefix": str(prefix)})
    )
    try:
        assert _wait(qapp, lambda: panel.report is not None)
        panel.web_rows[0].button.click()
        qapp.processEvents()
        assert started == []
        assert not panel.cleared_once
        assert list((profile / "Cache").iterdir())
    finally:
        panel.deleteLater()
