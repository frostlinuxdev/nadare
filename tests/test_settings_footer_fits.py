# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings' footer buttons keep their whole text at the smallest window (b23-rc4 click-through:
"ıstall or remo", "eport a proble", "ve chang" next to a long "Saved in …" path)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_footer_buttons_are_not_cut_at_the_minimum_width(tmp_path, monkeypatch, qapp):
    long_dir = tmp_path / ("a-long-config-folder-name-" * 3)
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(long_dir / key.lower()))
    cfg = Config.load(long_dir / "config.json")
    cfg.add_profile("nikke", backend="launcher-nikke", title="NIKKE")
    cfg.save()
    window = MainWindow(cfg)
    window.show()
    window.resize(window.minimumWidth(), 952)
    try:
        window.open_game("nikke")
        window.open_settings()
        for _ in range(10):
            qapp.processEvents()
        page = window.settings
        buttons = [page.remove_button, page.report_button, page.logs_button, page.save_button]
        for button in buttons:
            assert button.width() >= button.sizeHint().width() - 2, button.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
