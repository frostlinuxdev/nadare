# SPDX-License-Identifier: GPL-3.0-or-later
"""The Remove dialog stays readable with long paths (b23-rc4 click-through, 2026-11-11).

Each checkbox was one long line (path and reason included), so the dialog grew wider than the
screen and cut "remove it yourself if …" off; the game-folder/prefix boxes sat below the buttons.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox, QMessageBox
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_checkbox_lines_are_short_and_the_trees_sit_above_the_buttons(tmp_path, qapp, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        long_name = "a-rather-long-folder-name-" * 3
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / long_name / key))
    prefix = tmp_path / ("another-long-folder-name-" * 3) / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("nte-launcher", backend="launcher-nte", title="Neverness", prefix=str(prefix))
    cfg.save()
    window = MainWindow(cfg)
    seen = {}

    def fake_exec(box):
        checks = box.findChildren(QCheckBox)
        seen["lines"] = [line for c in checks for line in c.text().splitlines()]
        buttons = box.findChild(QDialogButtonBox)
        layout = box.layout()
        rows = {}
        for i in range(layout.count()):
            r, _c, _rs, _cs = layout.getItemPosition(i)
            item = layout.itemAt(i)
            widget = item.widget()
            if widget is buttons:
                rows["buttons"] = r
            elif widget is not None and widget.findChildren(QCheckBox):
                rows.setdefault("trees", r)
        seen["rows"] = rows
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("nte-launcher")
    finally:
        window.close()
        window.deleteLater()
    words = [line for line in seen["lines"] if not line.strip().startswith(("/", "~"))]
    assert words and max(len(line) for line in words) <= 100, max(words, key=len)
    assert any(line.strip() == str(prefix) for line in seen["lines"])  # the path on its own line
    assert seen["rows"]["trees"] < seen["rows"]["buttons"]
