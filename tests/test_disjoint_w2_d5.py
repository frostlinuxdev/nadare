# SPDX-License-Identifier: GPL-3.0-or-later
"""W2-D5: a failed file check must leave a trace after the next status probe."""

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


def test_failed_verify_is_not_erased_by_the_next_status(tmp_path, qapp, monkeypatch):
    shown: list[tuple] = []
    monkeypatch.setattr(
        operations_mod.QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: shown.append(a[1:3])),
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="direct-bd2", title="Alpha")
    window = MainWindow(config)
    try:
        window.open_game("alpha")
        window._on_result("alpha", Status("installed"))
        window._on_result(
            "alpha", {"verified": False, "message": "integrity check failed: files"}
        )
        assert shown and shown[0][0] == "File check failed", shown
        window._on_result("alpha", Status("installed"))
        seen = {
            "snapshot": window._snapshots["alpha"]["status"].state,
            "page_badge": window.game.badge.text(),
            "page_primary": window.game.primary.text(),
            "detail": window.game.detail_label.text(),
        }
        assert seen["page_badge"] == "ERROR", seen
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
