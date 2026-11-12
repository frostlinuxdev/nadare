# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed Remember-me undo must not claim the setting was turned off."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI deps
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import main_window as mw
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_a_failed_remember_me_undo_does_not_turn_it_off(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(
        mw.BackendController, "start",
        lambda self, profile, operation, payload=None: True,
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("other", backend="wine", title="Other")
    config.add_profile("p", backend="stove-czn", title="P", remember_me=True)
    window = MainWindow(config)
    shown: list[str] = []
    monkeypatch.setattr(
        mw.secrets, "SecretStore",
        lambda **kwargs: SimpleNamespace(secure=False),
    )
    monkeypatch.setattr(
        config, "save",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    window._remember_notice["p"] = "Remember me unavailable"
    try:
        window._show_remember_notice("p")
        assert window.config.profiles["p"]["remember_me"] is True
        assert "turned off" not in shown[-1]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
