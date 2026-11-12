# SPDX-License-Identifier: GPL-3.0-or-later
"""A library the user removed must not be adopted from a scan already running."""
from __future__ import annotations

import json
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication

from nadare.core import libraries
from nadare.core.config import Config
from nadare.gui.libraries_panel import LibrariesPanel

app = QApplication.instance() or QApplication([])


def _settle():
    for _ in range(3):
        assert QThreadPool.globalInstance().waitForDone(10_000)
        app.processEvents()


def test_a_library_removed_during_the_scan_is_not_adopted(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = Config.load(tmp_path / "config.json")
    lib = tmp_path / "Games"
    game = lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    manifest = {
        "schema": libraries.MANIFEST_SCHEMA, "backend": "direct-bd2",
        "title": "Brown Dust 2", "install": "install", "made_by_nadare": ["install"],
    }
    (game / libraries.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    gate, real_scan = threading.Event(), libraries.scan

    def slow_scan(config, root):
        gate.wait(10)
        return real_scan(config, root)

    monkeypatch.setattr(libraries, "scan", slow_scan)
    asked = []
    panel = LibrariesPanel(choose=lambda items: asked.append(items) or [True] * len(items))
    try:
        panel.load(cfg)
        assert panel.add_folder(str(lib)) == ""
        assert panel.searching
        panel.remove(lib)  # removed before the background scan returns
        gate.set()
        _settle()
        assert asked == []                # the removed library must not be offered
        assert "bd2" not in cfg.profiles  # and its game must not be added back
    finally:
        panel.deleteLater()
