# SPDX-License-Identifier: GPL-3.0-or-later
"""Move and Remove are reachable from the game page and the library card (b21 polish)."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.library import GameCard, LibraryPage
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_the_game_page_has_a_more_menu_with_move_and_remove(qapp):
    view = GameView()
    view.set_profile("p", {"title": "P"})
    assert view.more_button.accessibleName() == "More"
    assert _texts(view.more_menu) == ["Move game…", "Uninstall or remove…"]
    view.deleteLater()


def test_the_menu_items_emit_their_requests(qapp):
    view = GameView()
    view.set_profile("p", {"title": "P"})
    seen: list[tuple[str, str]] = []
    view.move_requested.connect(lambda n: seen.append(("move", n)))
    view.remove_requested.connect(lambda n: seen.append(("remove", n)))
    for action in view.more_menu.actions():
        action.trigger()
    assert seen == [("move", "p"), ("remove", "p")]
    view.deleteLater()


def test_remove_from_the_game_page_runs_the_same_confirmation(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="Game P")
    window = MainWindow(config)
    asked: list[str] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or 0)
    window.open_game("p")
    window.game.remove_requested.emit("p")
    assert len(asked) == 1 and "Game P" in asked[0]
    assert "p" in config.profiles  # nothing was removed: the box was dismissed
    window.close()


def test_move_opens_settings_on_the_tab_with_the_move_button(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="Game P")
    window = MainWindow(config)
    window.open_game("p")
    window.game.move_requested.emit("p")
    tabs = window.settings.tabs
    assert window.stack.currentWidget() is window.settings
    assert tabs.widget(tabs.currentIndex()).isAncestorOf(window.settings.move_button)
    window.close()


def test_a_library_card_has_a_remove_item_in_its_right_click_menu(qapp):
    page = LibraryPage()
    page.refresh({"a": {"backend": "wine", "title": "A"}})
    removed: list[str] = []
    page.remove_game.connect(removed.append)
    card = page._cards["a"]
    assert isinstance(card, GameCard)
    menu = page.card_menu("a")
    assert _texts(menu) == ["Open", "Uninstall or remove…"]
    menu.actions()[1].trigger()
    assert removed == ["a"]
    page.deleteLater()


def test_menus_are_styled_for_the_dark_theme():
    """The VM showed a white menu with near-invisible text: QMenu needs its own rules."""
    from nadare.gui.theme import stylesheet

    css = stylesheet()
    assert "QMenu {" in css and "QMenu::item:selected" in css and "QMenu::item:disabled" in css


def test_the_more_button_has_no_second_arrow(qapp):
    from nadare.gui.theme import stylesheet

    assert "QPushButton::menu-indicator" in stylesheet()
