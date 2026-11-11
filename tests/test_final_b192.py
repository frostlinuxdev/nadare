# SPDX-License-Identifier: GPL-3.0-or-later
"""A transient failed version check must not hide updates for six hours."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
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

def test_a_failed_version_check_is_retried_instead_of_waiting_six_hours(
    tmp_path, qapp, monkeypatch
):
    monkeypatch.setattr(
        mw.BackendController, "start",
        lambda self, profile, operation, payload=None: True,
    )
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = True
    config.add_profile("p", backend="direct-pgr", title="P",
                       install_dir=str(tmp_path / "game"), prefix=str(tmp_path / "prefix"))
    window = MainWindow(config, profile="p")
    # conftest disables automatic per-profile checks; use the real throttle here.
    monkeypatch.setattr(MainWindow, "_maybe_start_check_update",
                        MainWindow._real_maybe_start_check_update)
    started: list[str] = []
    monkeypatch.setattr(window, "_start",
                        lambda p, op, payload=None, silent=False:
                        started.append(op) or True)
    try:
        assert window._maybe_start_check_update("p") is True
        assert "check_update" in started
        window._on_failed("p", "check_update", "offline")
        started.clear()
        assert window._maybe_start_check_update("p") is True, (
            "a failed check must be retried, not throttled for six hours"
        )
        assert "check_update" in started
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
