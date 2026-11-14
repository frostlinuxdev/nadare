# SPDX-License-Identifier: GPL-3.0-or-later
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001 - GUI extras may be missing
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_failure_of_a_removed_profiles_worker_does_not_hit_the_readded_game(
    tmp_path, qapp, monkeypatch
):
    config = Config.load(tmp_path / "config.json")
    config.data["notify_when_done"] = False
    config.add_profile("g", backend="wine", title="Old game")
    window = MainWindow(config)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    try:
        window._start("g", "install")  # records the old worker's generation
        window._busy["g"] = "install"
        del window.config.profiles["g"]
        window.refresh_library()  # generation +1: removed
        window.config.add_profile("g", backend="wine", title="New game")
        window.refresh_library()  # generation +1: re-added
        window._current_profile = "elsewhere"
        window._on_failed("g", "install", "disk full")
        assert "g" not in window._snapshots, "old failure was written onto the new game"
        assert "g" not in window._status_errors
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
