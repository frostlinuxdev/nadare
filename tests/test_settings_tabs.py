# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings is split into tabs (General / Game / Launcher) instead of one long page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QLabel
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


@pytest.fixture
def page(qapp, tmp_path):
    widget = SettingsPage()
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    widget.load(config, "sample")
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _tab_of(page, widget) -> str:
    for index in range(page.tabs.count()):
        if page.tabs.widget(index).isAncestorOf(widget):
            return page.tabs.tabText(index)
    return ""


def test_the_tabs_are_general_game_and_nadare(page):
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == [
        "General",
        "Game",
        "Nadare (all games)",
    ]


def test_each_setting_lives_on_its_tab(page):
    assert _tab_of(page, page.title) == "General"
    assert _tab_of(page, page.background) == "General"
    assert _tab_of(page, page.remember) == "General"
    assert _tab_of(page, page.install_dir) == "Game"
    assert _tab_of(page, page.runner) == "Game"
    assert _tab_of(page, page.option_widget("renderer")) == "Game"
    assert _tab_of(page, page.language) == "Nadare (all games)"
    assert _tab_of(page, page.default_monitor) == "Nadare (all games)"
    assert _tab_of(page, page.open_config_button) == "Nadare (all games)"


def test_the_footer_stays_outside_the_tabs(page):
    for button in (page.remove_button, page.report_button, page.logs_button):
        assert _tab_of(page, button) == ""


def test_the_tab_bar_draws_no_white_base_line(page):
    assert not page.tabs.tabBar().drawBase()


def test_loading_a_game_starts_on_the_general_tab(page, tmp_path):
    page.tabs.setCurrentIndex(2)
    config = Config.load(tmp_path / "config2.json")
    config.add_profile("other", backend="wine")
    page.load(config, "other")
    assert page.tabs.currentIndex() == 0


def test_the_tab_bar_is_styled_for_the_dark_theme(qapp):
    from nadare.gui import theme

    theme.apply_theme(qapp)
    sheet = qapp.styleSheet()
    assert "QTabBar::tab" in sheet and "QTabBar::tab:selected" in sheet


def test_the_nadare_tab_says_it_is_about_nadare_and_every_game(page):
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "NADARE — ALL GAMES" in labels
    assert any("apply to every game in Nadare" in text for text in labels)
    assert "LAUNCHER" not in labels
