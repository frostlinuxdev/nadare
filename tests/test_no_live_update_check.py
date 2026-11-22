# SPDX-License-Identifier: GPL-3.0-or-later
"""#1024: opening a direct game in a window test starts no live version check."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def test_opening_a_direct_game_starts_no_check_update(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    import time

    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from nadare.core import artwork
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    # The hero fetch is not what this test checks, and it would use the network.
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)
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
        # Fresh news: _after_status goes on to the hero fetch instead of the feed.
        window._snapshots.setdefault("p", {})["news_at"] = time.monotonic()
        window._after_status("p")
        QThreadPool.globalInstance().waitForDone(3000)
        app.processEvents()
        assert "check_update" not in started
        assert "media" in started, "the game page still asks for its hero art"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
