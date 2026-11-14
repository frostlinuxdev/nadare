# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-7: Settings must not show the DW-Proton default when wine_binary will run."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from nadare.core import runners
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage


def test_settings_default_matches_the_actual_wine_binary(tmp_path, monkeypatch):
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "wuwa",
        backend="direct-wuwa",
        title="Wuthering Waves",
        wine_binary="/usr/bin/wine",
        prefix=str(tmp_path / "pfx"),
    )
    page = SettingsPage()
    try:
        page.load(cfg, "wuwa")
        text = page.runner.currentText()
        print("Settings runner row:", repr(text), "| wine_binary: /usr/bin/wine")
        assert "Default · recommended" not in text, (
            "Settings promises the recommended DW-Proton while the profile runs wine_binary"
        )
    finally:
        page.deleteLater()
        qapp.processEvents()
