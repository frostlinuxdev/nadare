# SPDX-License-Identifier: GPL-3.0-or-later
"""#1024: opening a direct game in a window test starts no live version check."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def test_opening_a_direct_game_starts_no_check_update(tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.profiles["p"] = {"backend": "direct-pgr", "title": "PGR",
                            "install_dir": str(tmp_path / "pgr")}
    window = MainWindow(config)
    started: list[str] = []
    real_start = window._start

    def spy(profile, operation, *args, **kwargs):
        started.append(operation)
        return real_start(profile, operation, *args, **kwargs)

    window._start = spy
    try:
        window.open_game("p")
        window._after_status("p")
        QThreadPool.globalInstance().waitForDone(3000)
        app.processEvents()
        assert "check_update" not in started
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
