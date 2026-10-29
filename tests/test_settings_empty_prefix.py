# SPDX-License-Identifier: GPL-3.0-or-later
"""A game with no prefix set must not offer to move "." (b17 edge hunt, settings #5)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


def _page(tmp_path, qapp, monkeypatch, *, prefix, probed):
    status = SimpleNamespace(state="installed", data={"prefix": probed} if probed else {})
    monkeypatch.setattr(
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(status=lambda: status),
    )
    asked: list[str] = []
    shown: list[str] = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: asked.append("dialog") or ""
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: shown.append("QUESTION"))
    config = Config.load(tmp_path / "config.json")
    values = {"backend": "wine", "exe_name": "game.exe"}
    if prefix:
        values["prefix"] = prefix
    config.add_profile("winegame", **values)
    page = SettingsPage()
    page.load(config, "winegame")
    _settle(qapp)
    return page, asked, shown


def test_move_without_a_set_prefix_explains_instead_of_moving_dot(tmp_path, qapp, monkeypatch):
    default = str(tmp_path / "nadare-data" / "prefix")
    page, asked, shown = _page(tmp_path, qapp, monkeypatch, prefix="", probed=default)
    try:
        page.move_button.click()
        _settle(qapp)
        assert asked == [], "no folder dialog for a prefix that is not set"
        assert "QUESTION" not in shown
        assert len(shown) == 1
        assert default in shown[0], "it names the folder Nadare is actually using"
        assert "I moved it myself" in shown[0]
        assert page.move_button.isEnabled(), "the button is usable again afterwards"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_message_still_makes_sense_when_the_folder_is_unknown(tmp_path, qapp, monkeypatch):
    page, asked, shown = _page(tmp_path, qapp, monkeypatch, prefix="", probed="")
    try:
        page.move_button.click()
        _settle(qapp)
        assert asked == [] and len(shown) == 1
        assert "no prefix set" in shown[0] and "None" not in shown[0] and "''" not in shown[0]
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_profile_with_a_prefix_is_unaffected(tmp_path, qapp, monkeypatch):
    real = tmp_path / "pfx"
    (real / "drive_c").mkdir(parents=True)
    page, asked, shown = _page(tmp_path, qapp, monkeypatch, prefix=str(real), probed=str(real))
    try:
        page.move_button.click()
        _settle(qapp)
        assert asked == ["dialog"], "the normal flow still asks where to move it"
        assert not any("no prefix set" in s for s in shown)
    finally:
        page.deleteLater()
        qapp.processEvents()
