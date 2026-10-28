# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings UX fixes: game folder, locked prefix, visible help, display, runners."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QLabel
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import get
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _settle(qapp) -> None:
    """Let Settings' background "is it installed?" probe finish and deliver its answer."""
    from PyQt6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


def _config(tmp_path, kind: str, name: str = "sample", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend=kind, **profile)
    return config


def test_game_folder_is_only_shown_for_backends_that_use_it(tmp_path, qapp):
    page = SettingsPage()
    try:
        page.load(_config(tmp_path, "stove-czn"), "sample")
        assert not page.install_dir.isVisibleTo(page), "CZN derives its folder from the prefix"
        page.install_dir.setText("/tmp/ignored")
        page.save()
        saved = Config.load(tmp_path / "config.json").profile("sample")
        assert saved.get("install_dir") != "/tmp/ignored"

        page.load(_config(tmp_path, "arknights-yostar"), "sample")
        assert page.install_dir.isVisibleTo(page)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_installed_game_locks_the_wine_prefix(tmp_path, qapp):
    from nadare.backends.czn.backend import GAME_SUBDIR, SHIELD_NAME

    prefix = tmp_path / "prefix"
    shield = prefix / GAME_SUBDIR / "bin" / SHIELD_NAME
    shield.parent.mkdir(parents=True)
    shield.write_text("")

    page = SettingsPage()
    try:
        page.load(_config(tmp_path, "stove-czn", prefix=str(prefix)), "sample")
        _settle(qapp)
        editor = page.option_widget("prefix")
        assert editor.isReadOnly(), "changing the prefix after install hides the game"

        fresh = tmp_path / "fresh"
        page.load(_config(tmp_path, "stove-czn", prefix=str(fresh)), "sample")
        _settle(qapp)
        assert not page.option_widget("prefix").isReadOnly()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_option_help_is_visible_text_not_only_a_tooltip(tmp_path, qapp):
    config = _config(tmp_path, "stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        display = next(o for o in get("stove-czn").options if o.key == "display")
        labels = [label for label in page.findChildren(QLabel) if label.text() == display.help]
        assert labels, "the NVIDIA/X11 warning must be shown as text"
        assert labels[0].isVisibleTo(page)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_arknights_and_stella_offer_the_display_choice(tmp_path, qapp):
    choices = {"auto", "wayland", "x11"}
    for kind in ("arknights-yostar", "stellasora-yostar", "stove-czn"):
        display = next(o for o in get(kind).options if o.key == "display")
        assert {value for _label, value in display.choices} == choices, kind

    config = _config(tmp_path, "arknights-yostar")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("display")
        combo.setCurrentIndex(combo.findData("x11"))
        page.save()
        assert config.profile("sample")["display"] == "x11"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_custom_runner_that_does_not_exist_is_warned_about_on_save(tmp_path, qapp, monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.settings.QMessageBox.open", lambda self, *a: shown.append(self.text())
    )
    config = _config(tmp_path, "stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.runner.setEditText("not-a-real-runner")
        page.save()
        assert shown, "saving a missing custom runner must warn"
        assert "not installed" in shown[0].lower()
        assert "pick one from the list" in shown[0].lower()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_runner_aliases_are_not_labelled_missing(tmp_path, qapp, monkeypatch):
    from nadare.core import runners

    monkeypatch.setattr(runners, "all_runners", lambda: [])
    config = _config(tmp_path, "stove-czn", runner="latest")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert "Missing" not in page.runner.currentText()
    finally:
        page.deleteLater()
        qapp.processEvents()
