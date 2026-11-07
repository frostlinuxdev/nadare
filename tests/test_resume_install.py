# SPDX-License-Identifier: GPL-3.0-or-later
"""An interrupted install is RESUME, not a fresh INSTALL with a 127 GiB question."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _status(**data) -> Status:
    return Status(state="not_installed", message="installation incomplete", data=data)


def test_an_interrupted_install_offers_resume(qapp):
    view = GameView()
    view.set_profile("sample", {"backend": "direct-pgr"})
    try:
        view.set_status(_status(incomplete=True))
        assert view.primary.text() == "RESUME"
        assert view.primary.isEnabled()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_a_never_installed_game_still_says_install(qapp):
    view = GameView()
    view.set_profile("sample", {"backend": "direct-pgr"})
    try:
        view.set_status(Status(state="not_installed", message="not installed yet"))
        assert view.primary.text() == "INSTALL"
    finally:
        view.deleteLater()
        qapp.processEvents()


def _window(tmp_path, monkeypatch):
    from nadare.core.config import Config

    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    config = Config.load(tmp_path / "config.json")
    config.profiles["pgr"] = {"backend": "direct-pgr", "install_dir": str(tmp_path / "pgr-files")}
    return MainWindow(config)


def test_resume_asks_nothing(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(
            QMessageBox, "exec", lambda self: pytest.fail("RESUME must not ask for a download")
        )
        window._snapshot("pgr")["status"] = _status(incomplete=True)
        assert window._confirm_download("pgr") is True
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_fresh_install_names_the_folder(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    seen: list[str] = []
    try:
        monkeypatch.setattr(
            QMessageBox,
            "exec",
            lambda self: seen.append(self.text()) or QMessageBox.StandardButton.Cancel,
        )
        window._snapshot("pgr")["status"] = Status(state="not_installed")
        assert window._confirm_download("pgr") is False
        assert seen and str(tmp_path / "pgr-files") in seen[0]
        assert "GiB" in seen[0] or "GB" in seen[0]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
