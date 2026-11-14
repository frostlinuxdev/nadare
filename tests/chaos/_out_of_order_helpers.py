# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared setup for the GUI out-of-order probes (not a test module)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only without PyQt6
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_background_work(monkeypatch):
    """No startup probes, update/notice checks or found-copy thread in these tests."""
    for name in (
        "_probe_all",
        "_start_found_copies_check",
        "_start_update_check",
        "_start_notices_check",
        "_start_steam_shortcut_cleanup",
    ):
        monkeypatch.setattr(MainWindow, name, lambda self: None)


def make_window(tmp_path, profiles):
    config = Config.load(tmp_path / "config.json")
    config.data["notify_when_done"] = False
    for name, profile in profiles.items():
        config.add_profile(name, **profile)
    # No game is opened by the constructor: each test opens what it needs.
    config.data["active_profile"] = ""
    config.save()
    window = MainWindow(config)
    window.controller.start = lambda profile, operation, payload=None: True
    return window, config
