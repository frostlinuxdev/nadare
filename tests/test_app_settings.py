# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare's own settings open without a game (b21 polish)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import PAGE_SETTINGS, MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _window(tmp_path, *, games=()):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    for name in games:
        config.add_profile(name, backend="wine", title=name.upper())
    return config, MainWindow(config)


def test_settings_opens_with_no_games_instead_of_asking_for_one(tmp_path, qapp, monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2]))
    )
    _config, window = _window(tmp_path)
    window.open_settings()
    assert shown == []
    assert window.stack.currentIndex() == PAGE_SETTINGS
    assert window.settings.heading.text() == "Nadare settings"
    window.close()


def test_from_the_library_settings_are_nadares_not_the_last_games(tmp_path, qapp):
    _config, window = _window(tmp_path, games=("a", "b"))
    window.show_library()
    window.open_settings()
    assert window.settings.heading.text() == "Nadare settings"
    tabs = window.settings.tabs
    visible = [i for i in range(tabs.count()) if tabs.isTabVisible(i)]
    assert len(visible) == 1
    window.close()


def test_from_a_game_page_settings_are_that_games_with_every_tab(tmp_path, qapp):
    _config, window = _window(tmp_path, games=("a",))
    window.open_game("a")
    window.open_settings()
    assert "A" in window.settings.heading.text()
    tabs = window.settings.tabs
    assert all(tabs.isTabVisible(i) for i in range(tabs.count()))
    window.close()


def test_app_only_settings_save_without_a_game(tmp_path, qapp):
    config, window = _window(tmp_path)
    window.open_settings()
    page = window.settings
    page.check_updates.setChecked(True)
    page.check_notices.setChecked(False)
    page.hide_uninstalled.setChecked(True)
    page.save()
    assert config.data["check_updates"] is True
    assert config.data["check_notices"] is False
    assert config.data["sidebar_hide_uninstalled"] is True
    assert window.stack.currentIndex() != PAGE_SETTINGS  # left Settings after saving
    window.close()


def test_game_only_buttons_are_hidden_in_app_only_mode(tmp_path, qapp):
    _config, window = _window(tmp_path)
    window.open_settings()
    page = window.settings
    assert page.remove_button.isHidden()
    assert page.report_button.isHidden()
    assert page.logs_button.isHidden()
    window.close()


def test_a_game_page_shows_the_game_buttons_again_after_app_only(tmp_path, qapp):
    _config, window = _window(tmp_path, games=("a",))
    window.show_library()
    window.open_settings()
    window.settings.cancelled.emit()
    window.open_game("a")
    window.open_settings()
    assert not window.settings.remove_button.isHidden()
    window.close()


def test_the_subtitle_matches_what_the_page_is_about(tmp_path, qapp):
    _config, window = _window(tmp_path, games=("a",))
    page = window.settings
    window.show_library()
    window.open_settings()
    assert "game" not in page.subtitle.text().lower() or "every game" in page.subtitle.text()
    window.settings.cancelled.emit()
    window.open_game("a")
    window.open_settings()
    assert page.subtitle.text() == "How this game is installed, launched and displayed."
    window.close()
