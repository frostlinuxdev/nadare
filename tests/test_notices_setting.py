# SPDX-License-Identifier: GPL-3.0-or-later
"""The notices feed has its own setting, separate from update checks (b21 polish)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
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


def _page(tmp_path, **top):
    config = Config.load(tmp_path / "config.json")
    config.data.update(top)
    config.add_profile("g", backend="wine", title="G")
    page = SettingsPage()
    page.load(config, "g")
    return config, page


def test_settings_has_its_own_notices_checkbox(tmp_path, qapp):
    _config, page = _page(tmp_path)
    try:
        assert page.check_notices.isChecked()
        assert page.check_notices is not page.check_updates
        assert "safety" in page.check_notices.toolTip().lower()
    finally:
        page.deleteLater()


def test_the_two_checkboxes_save_independently(tmp_path, qapp):
    config, page = _page(tmp_path)
    try:
        page.check_updates.setChecked(False)
        page.check_notices.setChecked(True)
        page.save()
        assert config.data["check_updates"] is False
        assert config.data["check_notices"] is True
        page.check_notices.setChecked(False)
        page.save()
        assert config.data["check_notices"] is False
    finally:
        page.deleteLater()
