# SPDX-License-Identifier: GPL-3.0-or-later
"""Find games must name the skipped folder, not claim nothing was found."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import libraries
from nadare.core.config import Config
from nadare.gui.libraries_panel import LibrariesPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_skipped_library_game_is_explained(qapp, tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = Config.load(tmp_path / "config.json")
    lib = tmp_path / "Games"
    game = lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / libraries.MANIFEST_NAME).write_text(
        json.dumps({"schema": 99, "backend": "direct-bd2"}), encoding="utf-8"
    )
    libraries.add_library(cfg, lib)
    cfg.save()

    panel = LibrariesPanel(choose=lambda items: [True] * len(items))
    try:
        panel.load(cfg)
        assert panel.find_games() == []
        assert "No new games" in panel.message.text()  # the user is told nothing is there
        assert "skipped" in panel.message.text()       # but scan() knew why; this fails today
    finally:
        panel.deleteLater()
