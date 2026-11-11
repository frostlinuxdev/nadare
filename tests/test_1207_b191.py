# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening a game must not run the bubblewrap readiness probe on the GUI thread."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI deps
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import main_window as mw
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_opening_a_sandbox_game_does_not_wait_for_the_bwrap_probe(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(
        mw.BackendController,
        "start",
        lambda self, profile, operation, payload=None: True,
    )

    def slow_ready():
        time.sleep(0.6)
        return True

    monkeypatch.setattr(mw.gamesandbox, "ready", slow_ready)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "pgr",
        backend="direct-pgr",
        title="PGR",
        install_dir=str(tmp_path / "game"),
        prefix=str(tmp_path / "prefix"),
    )
    window = MainWindow(config, profile="pgr")
    try:
        started = time.monotonic()
        window.open_game("pgr")
        elapsed = time.monotonic() - started
        assert elapsed < 0.25, f"open_game blocked the GUI for {elapsed:.2f}s"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
