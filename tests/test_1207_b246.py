# SPDX-License-Identifier: GPL-3.0-or-later
"""The window must follow a desktop text-size change, not keep its construction-time sheet."""

from __future__ import annotations

import os
import re
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import theme
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _base_px(css: str) -> int:
    if "* {" not in css:
        return 0
    block = css.split("* {", 1)[1].split("}", 1)[0]
    match = re.search(r"font-size:\s*(\d+)px", block)
    return int(match.group(1)) if match else 0


def test_the_window_does_not_keep_the_old_sheet_after_a_font_change(tmp_path, qapp):
    original = QFont(qapp.font())
    window = None
    try:
        small = QFont(qapp.font())
        small.setPointSizeF(10)
        qapp.setFont(small)
        for _ in range(5):
            qapp.processEvents()
            time.sleep(0.01)
        theme.apply_theme(qapp)
        window = MainWindow(Config.load(tmp_path / "config.json"))
        own = window.styleSheet()
        assert own == "" or _base_px(own) == 13, "the window is themed at the small size"

        large = QFont(qapp.font())
        large.setPointSizeF(20)
        qapp.setFont(large)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _base_px(qapp.styleSheet()) < 20:
            qapp.processEvents()
            time.sleep(0.01)
        assert _base_px(qapp.styleSheet()) >= 20, "the application stylesheet never refreshed"
        own = window.styleSheet()
        assert own == "" or _base_px(own) >= 20, "the window kept the old, smaller stylesheet"
    finally:
        qapp.setFont(original)
        if window is not None:
            window.close()
            window.deleteLater()
        qapp.processEvents()
