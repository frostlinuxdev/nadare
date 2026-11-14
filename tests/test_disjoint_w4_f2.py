# SPDX-License-Identifier: GPL-3.0-or-later
"""W4-F2: the GUI install check must refuse what cannot fit and use the chosen WuWa quality."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from nadare.core import diskspace
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import confirm as confirm_mod


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_install_is_refused_when_the_chosen_quality_does_not_fit(
    tmp_path, qapp, monkeypatch
):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "wuwa",
        backend="direct-wuwa",
        title="Wuthering Waves",
        install_dir=str(tmp_path / "games" / "install"),
        wuwa_quality="sd",
    )
    window = MainWindow(config)
    try:
        monkeypatch.setattr(diskspace, "free_bytes", lambda path: 70 * 2**30)
        monkeypatch.setattr(
            confirm_mod.QMessageBox,
            "exec",
            lambda self: QMessageBox.StandardButton.Yes,
        )
        fits = window._confirm_download("wuwa")
        config.profiles["wuwa"]["wuwa_quality"] = "hd"
        too_big = window._confirm_download("wuwa")
        assert (fits, too_big) == (True, False), (
            "SD (58 GiB) fits in 70 GiB, HD (80 GiB) does not",
            fits,
            too_big,
        )
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
