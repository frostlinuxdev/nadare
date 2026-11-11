# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b221: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_restore_defaults_resets_the_per_game_monitor(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="not_installed")
        ),
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", monitor="DP-1")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("monitor")
        assert combo is not None and combo.currentData() == "DP-1"
        page.restore_defaults.click()
        assert combo.currentData() == "", "Restore defaults must put Monitor back to Auto"
    finally:
        page.deleteLater()
        qapp.processEvents()
