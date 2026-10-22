# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings page: Restore defaults for the generated Game options card (#269)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path, kind: str, name: str = "sample", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend=kind, **profile)
    return config


def _dispose(page: SettingsPage, qapp) -> None:
    page.deleteLater()
    qapp.processEvents()


def test_restore_button_is_hidden_for_a_backend_without_options(
    tmp_path, qapp, monkeypatch
):
    from nadare.core.backends import get

    monkeypatch.setattr(get("steam"), "options", ())
    config = _config(tmp_path, "steam")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert not page.restore_defaults.isVisibleTo(page)
    finally:
        _dispose(page, qapp)


def test_restore_button_is_visible_when_the_backend_has_options(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert page.restore_defaults.isVisibleTo(page)
        assert page.restore_defaults.text() == "Restore defaults"
        assert page.restore_defaults.objectName() == "GhostButton"
        assert "Nothing is saved until you press Save changes." in (
            page.restore_defaults.toolTip()
        )
    finally:
        _dispose(page, qapp)


def test_restore_resets_choice_bool_and_text_without_saving(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    config.save()
    path = config.path
    before = path.read_text(encoding="utf-8")

    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("renderer")
        target = next(i for i in range(combo.count()) if combo.itemData(i) != "auto")
        combo.setCurrentIndex(target)
        page.option_widget("vcredist").setChecked(True)
        page.option_widget("exe_name").setText("x.exe")

        page.restore_defaults.click()

        assert combo.currentData() == "auto"
        assert not page.option_widget("vcredist").isChecked()
        assert page.option_widget("exe_name").text() == ""
        assert path.read_text(encoding="utf-8") == before, "Restore must not save"
    finally:
        _dispose(page, qapp)


def test_restore_resets_int_to_the_option_default(tmp_path, qapp):
    config = _config(tmp_path, "arknights-yostar")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        spin = page.option_widget("download_workers")
        assert spin.value() == 6
        spin.setValue(3)

        page.restore_defaults.click()

        assert spin.value() == 6
    finally:
        _dispose(page, qapp)


def test_saving_restored_defaults_does_not_store_option_keys(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("renderer")
        combo.setCurrentIndex(
            next(i for i in range(combo.count()) if combo.itemData(i) != "auto")
        )
        page.option_widget("vcredist").setChecked(True)
        page.option_widget("exe_name").setText("x.exe")
        page.restore_defaults.click()

        page.save()

        profile = config.profile("sample")
        assert "renderer" not in profile
        assert "vcredist" not in profile
        assert "exe_name" not in profile
    finally:
        _dispose(page, qapp)


def test_saving_a_restored_int_default_does_not_store_the_key(tmp_path, qapp):
    config = _config(tmp_path, "arknights-yostar")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.option_widget("download_workers").setValue(3)
        page.restore_defaults.click()
        assert page.option_widget("download_workers").value() == 6

        page.save()

        assert "download_workers" not in config.profile("sample")
    finally:
        _dispose(page, qapp)


def test_non_default_value_still_saves(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("renderer")
        target = next(i for i in range(combo.count()) if combo.itemData(i) != "auto")
        combo.setCurrentIndex(target)

        page.save()

        assert config.profile("sample")["renderer"] == combo.itemData(target)
    finally:
        _dispose(page, qapp)


def test_back_after_restore_asks_to_discard_when_it_differs(tmp_path, qapp, monkeypatch):
    config = _config(tmp_path, "wine", renderer="vulkan")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        asked = []
        monkeypatch.setattr(
            "nadare.gui.settings.QMessageBox.question",
            lambda *args, **kwargs: (
                asked.append(args),
                QMessageBox.StandardButton.No,
            )[1],
        )

        page.restore_defaults.click()
        assert page.option_widget("renderer").currentData() == "auto"
        page._request_back()

        assert asked, "Back must ask to discard the restored defaults"
    finally:
        _dispose(page, qapp)
