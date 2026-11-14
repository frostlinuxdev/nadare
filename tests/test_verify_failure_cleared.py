# SPDX-License-Identifier: GPL-3.0-or-later
"""A kept "File check failed" (#1304 W2-D5) ends when fresh files arrive or the game goes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import operations as operations_mod


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("operation", ["update", "install", "repair"])
def test_fresh_files_clear_a_failed_check(tmp_path, qapp, monkeypatch, operation):
    monkeypatch.setattr(operations_mod.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="direct-bd2", title="Alpha")
    window = MainWindow(config)
    monkeypatch.setattr(window, "_start", lambda *a, **k: True)
    try:
        window.open_game("alpha")
        window._on_result("alpha", Status("installed"))
        window._on_result("alpha", {"verified": False, "message": "integrity check failed"})
        assert window._snapshots["alpha"]["status"].state == "error"
        window._on_succeeded("alpha", operation, 0)
        window._on_result("alpha", Status("installed"))
        assert window._snapshots["alpha"]["status"].state == "installed"
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
