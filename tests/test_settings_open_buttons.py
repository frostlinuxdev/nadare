# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: open the game folder, the Wine prefix and the config file from the page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_gui
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(
        settings_gui.QDesktopServices, "openUrl", lambda url: urls.append(url.toLocalFile()) or True
    )
    return urls


@pytest.fixture
def page(qapp):
    widget = SettingsPage()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _load(page, tmp_path, backend="wine", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend=backend, **profile)
    page.load(config, "sample")
    return config


def test_open_game_folder_opens_the_folder_in_the_field(tmp_path, page, opened):
    game = tmp_path / "game"
    game.mkdir()
    _load(page, tmp_path, install_dir=str(game))
    page.open_game_button.click()
    assert opened == [str(game)]


def test_open_game_folder_uses_the_unsaved_text_in_the_field(tmp_path, page, opened):
    other = tmp_path / "other"
    other.mkdir()
    _load(page, tmp_path, install_dir=str(tmp_path / "game"))
    page.install_dir.setText(str(other))
    page.open_game_button.click()
    assert opened == [str(other)]


def test_open_game_folder_with_nothing_there_says_so_and_opens_nothing(tmp_path, page, opened):
    _load(page, tmp_path, install_dir=str(tmp_path / "not-yet"))
    page.open_game_button.click()
    assert opened == []
    assert "does not exist" in page.note.text()


def test_open_game_folder_with_an_empty_field_opens_nothing(tmp_path, page, opened):
    _load(page, tmp_path)
    page.open_game_button.click()
    assert opened == []
    assert page.note.text()


def test_open_prefix_opens_the_prefix_folder(tmp_path, page, opened):
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    _load(page, tmp_path, prefix=str(prefix))
    page.open_prefix_button.click()
    assert opened == [str(prefix)]


def test_open_prefix_falls_back_to_the_folder_the_status_reports(tmp_path, page, opened):
    own = tmp_path / "own-prefix"
    own.mkdir()
    _load(page, tmp_path)
    page._probed_prefix = str(own)
    page.open_prefix_button.click()
    assert opened == [str(own)]


def test_open_config_file_opens_the_config_file(tmp_path, page, opened):
    config = _load(page, tmp_path)
    config.save()
    page.open_config_button.click()
    assert opened == [str(config.path)]


def test_buttons_follow_the_backend(tmp_path, page, opened):
    _load(page, tmp_path, backend="stove-czn")
    page.show()
    assert page.open_game_button.isHidden()
    assert not page.open_prefix_button.isHidden()
    page.hide()
