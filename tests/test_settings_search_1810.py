# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings search (#1810): filter every tab, restore exactly what was shown."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeySequence
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QFrame
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


def _page(tmp_path, kind: str = "wine", name: str = "sample") -> SettingsPage:
    config = Config.load(tmp_path / f"{name}.json")
    config.add_profile(name, backend=kind)
    page = SettingsPage()
    page.load(config, name)
    return page


@pytest.fixture
def page(qapp, tmp_path):
    widget = _page(tmp_path)
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _shown(page: SettingsPage, widget) -> bool:
    """Whether ``widget`` is visible inside its own tab (the window may not be shown)."""
    for index in range(page.tabs.count()):
        tab = page.tabs.widget(index)
        if tab.isAncestorOf(widget):
            return widget.isVisibleTo(tab)
    return widget.isVisibleTo(page)


def _card(widget):
    current = widget
    while current is not None:
        if isinstance(current, QFrame) and current.objectName() == "Card":
            return current
        current = current.parentWidget()
    raise AssertionError("widget is not inside a Settings card")


# ---- matching ---------------------------------------------------------------------------
def test_a_label_word_shows_its_row_and_hides_an_unrelated_one(page):
    page.search.setText("game name")
    assert _shown(page, page.title)
    assert not _shown(page, page.background)
    # Also inside the same card: only the "Game name" row stays.
    assert not _shown(page, page.profile_name)


def test_a_hint_word_shows_the_row_it_belongs_to(page):
    page.search.setText("not to one installation")
    assert _shown(page, page.default_monitor)
    assert _shown(page, page.default_monitor_hint)
    assert not _shown(page, page.title)


def test_a_card_title_word_keeps_the_whole_card(page):
    page.search.setText("appearance")
    assert _shown(page, page.background)
    assert not _shown(page, page.title)


@pytest.mark.parametrize("query", ["NAME game", "game NAME"])
def test_words_match_in_any_order_and_any_case(page, query):
    page.search.setText(query)
    assert _shown(page, page.title)
    assert not _shown(page, page.background)


def test_every_word_must_match(page):
    page.search.setText("game zebra")
    assert not _shown(page, page.title)
    assert _shown(page, page.search_empty)


# ---- tabs -------------------------------------------------------------------------------
def test_tab_titles_show_the_hit_count(page):
    page.search.setText("custom background")
    titles = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert titles == ["General (1)", "Game (0)", "Nadare (all games) (0)"]


def test_current_tab_switches_to_the_first_tab_with_hits(page):
    page.tabs.setCurrentIndex(1)
    page.search.setText("discord")
    assert page.tabs.currentIndex() == 2
    assert page.tabs.tabText(2) == "Nadare (all games) (1)"


def test_no_hits_shows_one_line_and_keeps_the_tabs(page):
    base = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    page.tabs.setCurrentIndex(1)
    page.search.setText("zzzznothing")
    assert page.search_empty.isVisibleTo(page)
    assert page.search_empty.text() == "Nothing matches “zzzznothing”"
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == base
    assert page.tabs.currentIndex() == 1
    assert not _shown(page, page.title)
    # It hides again as soon as the query matches, and on clear.
    page.search.setText("game name")
    assert not page.search_empty.isVisibleTo(page)
    page.search.setText("zzzznothing")
    assert page.search_empty.isVisibleTo(page)
    page.search.clear()
    assert not page.search_empty.isVisibleTo(page)
    assert page.search_empty.text() == ""


def test_an_empty_card_hides(page):
    page.search.setText("custom background")
    assert _shown(page, _card(page.background))
    assert not _shown(page, _card(page.title))


def test_app_only_search_does_not_count_or_switch_to_hidden_tabs(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    page = SettingsPage()
    try:
        page.load_app_only(config)
        page.search.setText("game name")
        assert page.tabs.isTabVisible(page._app_tab)
        assert not page.tabs.isTabVisible(0)
        assert not page.tabs.isTabVisible(1)
        assert page.tabs.currentIndex() == page._app_tab
        # Hidden tabs get no count and do not hide the no-hits line.
        assert page.tabs.tabText(0) == "General"
        assert page.tabs.tabText(1) == "Game"
        assert page.tabs.tabText(2) == "Nadare (all games)"
        assert page.search_empty.isVisibleTo(page)
        # Even when the visible tab has hits, the hidden tabs stay uncounted.
        page.search.setText("monitor")
        assert page.tabs.tabText(0) == "General"
        assert page.tabs.tabText(1) == "Game"
        assert page.tabs.tabText(2) == "Nadare (all games) (1)"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_page_hidden_row_never_matches_or_counts(tmp_path, qapp):
    page = _page(tmp_path, "steam")
    try:
        assert not _shown(page, page.install_dir), "Steam has no Game folder row"
        page.search.setText("game folder")
        # The hidden row's text (and its empty hint row) must not keep the card.
        assert not _shown(page, page.install_dir)
        assert not _shown(page, _card(page.install_dir))
        assert page.tabs.tabText(1) == "Game (0)"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_card_title_hit_counts_only_the_rows_the_page_shows(page):
    page.search.setText("game files")
    assert _shown(page, page.install_dir)
    assert _shown(page, page.runner)
    assert _shown(page, page.open_game_button)
    # Page-hidden rows and the empty place_box container do not add a count.
    assert not _shown(page, page.runner_behind)
    assert not _shown(page, page.remake_prefix_btn)
    assert page.tabs.tabText(1) == "Game (3)"


# ---- restoring --------------------------------------------------------------------------
def test_clear_restores_rows_but_not_a_row_the_page_hid(tmp_path, qapp):
    page = _page(tmp_path, "steam")
    try:
        assert not _shown(page, page.install_dir), "Steam has no Game folder row"
        row = page.option_widget("steam_app_id")
        page.search.setText("steam")
        assert _shown(page, row)
        # A row the search hid comes back; the Steam-hidden row does not.
        assert not _shown(page, page.background)
        page.search.clear()
        assert _shown(page, row)
        assert _shown(page, page.background)
        assert not _shown(page, page.install_dir)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_escape_clears_and_restores(qapp, tmp_path):
    page = _page(tmp_path)
    try:
        page.show()
        qapp.processEvents()
        page.search.setFocus()
        page.search.setText("game name")
        assert not _shown(page, page.background)
        QTest.keyClick(page.search, Qt.Key.Key_Escape)
        assert page.search.text() == ""
        assert _shown(page, page.title)
        assert _shown(page, page.background)
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_a_row_the_page_hides_during_a_search_stays_hidden_after_clear(page):
    page.show_advanced.setChecked(True)
    renderer = page.option_widget("renderer")
    assert _shown(page, renderer)
    page.search.setText("sandbox")
    assert not _shown(page, renderer)  # the search hid it
    page.show_advanced.setChecked(False)  # and now the page hides it too
    page.search.clear()
    assert not _shown(page, renderer)


def test_a_directly_hidden_row_stays_hidden_after_clear(page):
    # The page shows this row itself, then hides it while the search already hid it.
    page._set_widget_visible(page.move_to_flatpak_button, True)
    assert _shown(page, page.move_to_flatpak_button)
    page.search.setText("discord")
    assert not _shown(page, page.move_to_flatpak_button)
    page._set_widget_visible(page.move_to_flatpak_button, False)
    page.search.clear()
    assert not _shown(page, page.move_to_flatpak_button)


def test_a_direct_page_show_during_a_search_is_filtered_until_clear(page):
    page.search.setText("discord")
    assert _shown(page, page.discord_button)
    page._set_widget_visible(page.move_to_flatpak_button, True)
    assert not _shown(page, page.move_to_flatpak_button)
    page.search.clear()
    assert _shown(page, page.move_to_flatpak_button)


def test_showing_runner_behind_during_a_search_is_filtered_until_clear(page):
    # The Game files card stays visible through another row, but this hint does not match.
    page.search.setText("game folder")
    assert _shown(page, page.install_dir)
    assert not _shown(page, page.runner)
    page.runner_behind.setText("This game is set to an older build")
    page._set_widget_visible(page.runner_behind, True)
    assert not _shown(page, page.runner_behind)
    page.search.clear()
    assert _shown(page, page.runner_behind)


def test_showing_advanced_during_a_search_does_not_leak_rows(page):
    page.search.setText("sandbox")
    renderer = page.option_widget("renderer")
    assert not _shown(page, renderer)
    page.show_advanced.setChecked(True)
    assert _shown(page, page.option_widget("secure_mode"))
    assert not _shown(page, renderer)


def test_switching_profile_keeps_and_reapplies_the_query(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("one", backend="wine")
    config.add_profile("two", backend="wine")
    page = SettingsPage()
    try:
        page.load(config, "one")
        page.search.setText("monitor")
        assert _shown(page, page.option_widget("monitor"))
        assert not _shown(page, page.option_widget("secure_mode"))
        page.load(config, "two")
        assert page.search.text() == "monitor"
        assert _shown(page, page.option_widget("monitor"))
        assert not _shown(page, page.option_widget("secure_mode"))
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_opening_a_profile_after_an_app_only_search_restores_its_tab(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    page = SettingsPage()
    try:
        page.load_app_only(config)
        page.search.setText("monitor")
        assert page.tabs.currentIndex() == page._app_tab
        page.load(config, "sample")
        assert page.search.text() == "monitor"
        page.search.clear()
        assert page.tabs.currentIndex() == 0, "clear goes to the profile's tab, not the old one"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_switching_to_a_backend_that_hides_a_row_keeps_it_hidden(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("wine-game", backend="wine")
    config.add_profile("steam-game", backend="steam")
    page = SettingsPage()
    try:
        page.load(config, "wine-game")
        page._set_widget_visible(page.runner_behind, True)
        assert _shown(page, page.runner_behind)
        page.search.setText("monitor")
        assert not _shown(page, page.runner_behind)
        page.load(config, "steam-game")  # the page hides that row for Steam
        page.search.clear()
        assert not _shown(page, page.runner_behind)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_profile_switch_applies_the_search_once_when_the_load_ends(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("one", backend="wine")
    config.add_profile("two", backend="wine")
    page = SettingsPage()
    try:
        page.load(config, "one")
        page.show_advanced.setChecked(True)
        page.search.setText("monitor")
        calls: list[bool] = []
        original = page._search_apply

        def counting() -> None:
            calls.append(True)
            original()

        page._search_apply = counting
        page.load(config, "two")
        assert len(calls) == 1, "the query is applied once, after the load finished"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_switching_backends_keeps_and_reapplies_the_query(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("wine-game", backend="wine")
    config.add_profile("steam-game", backend="steam")
    page = SettingsPage()
    try:
        page.load(config, "wine-game")
        page.search.setText("monitor")
        assert _shown(page, page.option_widget("monitor"))
        page.load(config, "steam-game")
        assert page.search.text() == "monitor"
        # Steam has no Monitor option; only the all-games Monitor row matches.
        assert page.tabs.tabText(0) == "General (0)"
        assert page.tabs.tabText(1) == "Game (0)"
        assert page.tabs.tabText(2) == "Nadare (all games) (1)"
        assert _shown(page, page.default_monitor)
        page.load_app_only(config)
        assert page.search.text() == "monitor"
        assert page.tabs.currentIndex() == page._app_tab
        assert _shown(page, page.default_monitor)
        page.search.clear()
        assert page.tabs.currentIndex() == page._app_tab
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_ctrl_f_focuses_the_search_field(qapp, tmp_path):
    page = _page(tmp_path)
    try:
        page.show()
        qapp.processEvents()
        page.title.setFocus()
        QTest.keySequence(page.title, QKeySequence.StandardKey.Find)
        qapp.processEvents()
        assert QApplication.focusWidget() is page.search
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


# ---- translated UI ----------------------------------------------------------------------
def _write_locale(folder, code: str, data: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{code}.json").write_text(json.dumps(data), encoding="utf-8")


def test_a_translated_label_is_found_by_its_translated_text(tmp_path, qapp, monkeypatch):
    from nadare.core import i18n
    from nadare.gui import i18n as gui_i18n

    folder = tmp_path / "locales"
    _write_locale(folder, "en", {})
    _write_locale(
        folder,
        "xx",
        {
            "Search settings": "Einstellungen suchen",
            "Download speed limit (MB/s, 0 = none)": "Geschwindigkeitsgrenze",
        },
    )
    monkeypatch.setattr(i18n, "locale_dir", lambda: folder)
    monkeypatch.setattr(i18n, "_cache", {})
    monkeypatch.setattr(i18n, "_current", "en")
    gui_i18n.set_language("xx")
    page = _page(tmp_path)
    try:
        assert page.download_speed_limit.isVisibleTo(page.tabs.widget(2))
        page.search.setText("Geschwindigkeitsgrenze")
        assert _shown(page, page.download_speed_limit)
        page.search.clear()
        page.search.setText("Download speed limit")
        assert not _shown(page, page.download_speed_limit)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_tab_order_goes_search_then_tabs_then_rows(qapp, tmp_path):
    page = _page(tmp_path)
    try:
        page.show()
        qapp.processEvents()
        page.search.setFocus()
        QTest.keyClick(page.search, Qt.Key.Key_Tab)
        focus = QApplication.focusWidget()
        assert focus is page.tabs.tabBar(), "Tab from the field must reach the tab bar"
        QTest.keyClick(focus, Qt.Key.Key_Tab)
        focus = QApplication.focusWidget()
        assert focus is page.profile_name, "Tab from the tab bar must reach a setting row"
        assert _card(focus) is _card(page.title)
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_the_same_search_works_again_after_clearing(page):
    page.search.setText("monitor")
    assert _shown(page, page.option_widget("monitor"))
    page.search.clear()
    assert _shown(page, page.title)
    page.search.setText("monitor")
    assert _shown(page, page.option_widget("monitor"))
    assert page.tabs.tabText(2) == "Nadare (all games) (1)"


# ---- fix round 2 ------------------------------------------------------------------------
def _form_row(page, widget):
    """The tracked form and row whose field widget is ``widget``."""
    from nadare.gui.settings_parts.search import _row_widgets

    for info in page._search_cards:
        for form in info.forms():
            for row in range(form.rowCount()):
                _label, field = _row_widgets(form, row)
                if field is widget:
                    return form, row
    raise AssertionError("widget is not a row field")


def test_a_prefix_row_is_not_hidden_when_its_lock_note_hides(page):
    prefix = page.option_widget("prefix")
    page._set_prefix_locked(True)
    assert _shown(page, page._prefix_lock_note)
    page._set_prefix_locked(False)  # the real trigger: note and buttons hide
    assert page._prefix_lock_note.isHidden()
    page.search.setText("monitor")  # keeps the Game options card, not the prefix row
    assert _shown(page, page.option_widget("monitor"))
    assert not _shown(page, prefix), "a non-matching row must not leak into the results"


def test_an_open_row_matches_its_visible_button(tmp_path, qapp):
    page = _page(tmp_path, "stove-czn")
    try:
        assert page.open_game_button.isHidden()
        page.search.setText("open wine prefix")
        assert _shown(page, page.open_prefix_button)
        assert not _shown(page, page.open_game_button)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_place_box_row_matches_when_one_of_its_buttons_hides(page, monkeypatch):
    from nadare.core import gamefolders

    # Fixed paths: the place label shows the folder, and a tmp_path holds the user name
    # (/tmp/pytest-of-runner/... on GitHub CI), so a search for "runner" matched the row there.
    folder = Path("/games/game")
    importable = gamefolders.Placement(folder, None, False, {})
    managed = gamefolders.Placement(folder, Path("/games/library"), True, {})
    monkeypatch.setattr(gamefolders, "placement", lambda _config, _name: importable)
    page._show_placement()
    assert _shown(page, page.import_move_button)
    monkeypatch.setattr(gamefolders, "placement", lambda _config, _name: managed)
    page._show_placement()  # hides Move into a library, keeps the place label
    assert page.import_move_button.isHidden()
    page.search.setText("runner")  # keeps the Game files card, not the place row
    assert _shown(page, page.runner)
    assert not _shown(page, page.place_label), "a non-matching row must not leak"


def test_a_libraries_row_matches_when_its_message_hides(page):
    panel = page.libraries_panel
    panel._say("A note")
    assert _shown(page, panel.message)
    panel._say("")  # the real trigger: the note under the list hides
    page.search.setText("libraries")
    assert _shown(page, panel)


def test_a_disk_space_row_matches_when_a_category_collapses(page):
    category = page.disk_space_panel.categories["temp"]
    category._toggle(True)
    assert _shown(page, category.details)
    category._toggle(False)  # the real trigger: Details folds its rows away
    assert category.details.isHidden()
    page.search.setText("disk space")
    assert _shown(page, page.disk_space_panel)


def test_a_row_hidden_then_shown_by_the_page_during_a_search_comes_back(page):
    button = page.move_to_flatpak_button
    form, row = _form_row(page, button)
    page._set_widget_visible(button, True)
    page.search.setText("discord")
    assert not _shown(page, button)  # the search hid it
    page._set_widget_visible(button, False)  # the page hides it too
    page._set_widget_visible(button, True)  # and then shows it again
    assert not _shown(page, button), "the active search still hides it"
    page.search.clear()
    assert _shown(page, button)
    assert form.isRowVisible(row)


def test_words_can_split_between_a_row_and_its_hint(tmp_path, qapp):
    page = _page(tmp_path, "stove-czn")
    try:
        assert _shown(page, page.remember)
        page.search.setText("remember save")
        assert _shown(page, page.remember)
        assert _shown(page, page.remember_hint)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_ctrl_f_works_right_after_opening_settings_from_the_sidebar(tmp_path, qapp, monkeypatch):
    from nadare.gui import workers
    from nadare.gui.main_window import PAGE_SETTINGS, MainWindow

    monkeypatch.setattr(workers.BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.add_profile("sample", backend="wine", title="Sample")
    window = MainWindow(config)
    try:
        window.show()
        window.activateWindow()
        window.settings_button.click()
        qapp.processEvents()
        assert window.stack.currentIndex() == PAGE_SETTINGS
        # Clicking Settings leaves focus on the sidebar button, outside SettingsPage.
        window.settings_button.setFocus()
        qapp.processEvents()
        QTest.keySequence(window.settings_button, QKeySequence.StandardKey.Find)
        qapp.processEvents()
        assert QApplication.focusWidget() is window.settings.search
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_failed_load_does_not_leave_the_search_stuck(page, monkeypatch):
    page.search.setText("monitor")

    def boom(_profile):
        raise RuntimeError("load failed")

    monkeypatch.setattr(page._config, "profile", boom)
    with pytest.raises(RuntimeError):
        page.load(page._config, "sample")
    assert page._search_loading is False
    # A re-apply works again: a row the page just showed stays filtered by "monitor".
    page.show_advanced.setChecked(True)
    assert not _shown(page, page.option_widget("extra_env"))


def test_a_failed_app_only_load_does_not_leave_the_search_stuck(page, monkeypatch):
    page.search.setText("monitor")

    def boom(_config):
        raise RuntimeError("load failed")

    monkeypatch.setattr(page, "_load_app_values", boom)
    with pytest.raises(RuntimeError):
        page.load_app_only(page._config)
    assert page._search_loading is False


def test_auto_tab_switch_during_a_search_does_not_scan_disk_space(page):
    calls: list[bool] = []
    page.disk_space_panel.refresh_if_stale = lambda: calls.append(True)
    page.tabs.setCurrentIndex(1)
    page.search.setText("discord")
    assert page.tabs.currentIndex() == 2
    assert calls == [], "the search's own tab switch must not start a scan"
    page.tabs.setCurrentIndex(0)
    page.tabs.setCurrentIndex(2)  # a real click still refreshes
    assert calls == [True]


def test_showing_the_move_section_clears_the_search(page):
    page._set_widget_visible(page.move_button, True)
    page.search.setText("discord")
    assert not _shown(page, page.move_button)
    page.show_move_section()
    assert page.search.text() == ""
    assert _shown(page, page.move_button)


def test_tab_order_reaches_the_first_row_of_the_game_and_app_tabs(qapp, tmp_path):
    page = _page(tmp_path)
    try:
        page.show()
        qapp.processEvents()
        page.tabs.setCurrentIndex(1)
        page.tabs.tabBar().setFocus()
        qapp.processEvents()
        QTest.keyClick(page.tabs.tabBar(), Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is page.install_dir
        page.tabs.setCurrentIndex(2)
        page.tabs.tabBar().setFocus()
        qapp.processEvents()
        QTest.keyClick(page.tabs.tabBar(), Qt.Key.Key_Tab)
        assert QApplication.focusWidget() is page.download_speed_limit
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()
