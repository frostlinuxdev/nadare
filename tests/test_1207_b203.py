# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b203: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001 - GUI extras may be missing
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core import flatpakgl
from nadare.core.config import Config
from nadare.gui import main_window as mw


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_a_raising_graphics_install_does_not_wedge_the_banner(tmp_path, qapp, monkeypatch):
    bad = flatpakgl.GlCheck(False, "32-bit graphics missing", "flatpak install x", ("x",))
    monkeypatch.setattr(flatpakgl, "problem", lambda: bad)
    window = mw.MainWindow(Config.load(tmp_path / "config.json"))
    try:
        assert window._graphics_refs == ("x",)

        def boom(refs):
            raise OSError("flatpak-spawn died")

        monkeypatch.setattr(flatpakgl, "install_missing", boom)
        window._on_graphics_action()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and window._installing_graphics:
            qapp.processEvents()
            time.sleep(0.01)
        assert not window._installing_graphics, "banner stayed in 'Installing…'"
        assert window.graphics_banner.action_button.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
