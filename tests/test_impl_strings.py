# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-language wording for the library, game page, settings and wizard."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QLabel
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog
from nadare.gui.game_view import GameView
from nadare.gui.library import AddGameCard, GameCard, LibraryPage
from nadare.gui.main_window import MainWindow
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path):
    return Config.load(tmp_path / "config.json")


def test_sidebar_error_wraps_and_keeps_the_full_text_as_a_tooltip(tmp_path, qapp, monkeypatch):
    config = _config(tmp_path)
    config.add_profile("czn", backend="wine", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    monkeypatch.setattr("nadare.gui.main_window.QMessageBox.critical", lambda *a: None)
    try:
        window._on_remove_failed(
            "czn", "Permission denied: /home/user/.config/nadare/config.json"
        )
        text = window.side_status.text()
        assert "Could not remove Chaos Zero Nightmare" in text
        assert window.side_status.wordWrap()
        assert window.side_status.toolTip() == text
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_an_unplayed_game_is_not_labelled_now_playing(qapp):
    view = GameView()
    try:
        view.set_profile("czn", {"backend": "stove-czn", "title": "CZN"})
        assert view.kicker.text() == "NOT PLAYED YET"
        view.set_status(Status("not_installed"))
        assert view.kicker.text() == "NOT PLAYED YET"
        view.set_status(Status("running", running=True))
        assert view.kicker.text() == "NOW PLAYING"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_page_sidebar_says_viewing_until_the_game_runs(tmp_path, qapp):
    config = _config(tmp_path)
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    try:
        window.open_game("czn")
        assert window.side_status.text() == "Viewing Chaos Zero Nightmare"
        window._on_result("czn", Status("running", running=True))
        assert window.side_status.text() == "Playing Chaos Zero Nightmare"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_settings_and_removal_name_the_game_not_its_profile_key(tmp_path, qapp, monkeypatch):
    config = _config(tmp_path)
    config.add_profile("czn", backend="wine", title="Chaos Zero Nightmare")
    page = SettingsPage()
    page.load(config, "czn")
    try:
        assert page.heading.text() == "Settings — Chaos Zero Nightmare"
        assert any(label.text() == "Profile ID" for label in page.findChildren(QLabel))
    finally:
        page.deleteLater()
        qapp.processEvents()

    window = MainWindow(config)
    shown = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.information",
        lambda *a: shown.append(a),
    )
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.critical",
        lambda *a: shown.append(a),
    )
    try:
        window._busy["czn"] = "launch"
        window._on_remove_requested("czn")
        assert "Chaos Zero Nightmare is busy" in shown[-1][2]

        window._busy.pop("czn", None)
        window._on_remove_failed("czn", "Permission denied")
        assert window.side_status.text() == "Could not remove Chaos Zero Nightmare"
        assert "Could not remove Chaos Zero Nightmare" in shown[-1][2]

        window._on_game_removed("czn", None)
        assert window.side_status.text() == "Removed Chaos Zero Nightmare"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_wizard_separates_the_supported_games_from_bring_your_own_files(tmp_path, qapp):
    dialog = AddGameDialog(_config(tmp_path), data_root=tmp_path / "data")
    try:
        texts = [dialog.game_list.item(i).text() for i in range(dialog.game_list.count())]
        assert "ADVANCED (BRING YOUR OWN GAME FILES)" in texts
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert not any("Pick a supported game" in text for text in labels)
        assert any(text.startswith("Choose your game.") for text in labels)
        for title in ("Wine / Proton game", "Steam game", "Script"):
            assert any(text.startswith(title) for text in texts)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_profile_name_errors_say_what_to_type_and_how_to_fix_a_duplicate(tmp_path, qapp):
    config = _config(tmp_path)
    config.add_profile("mine", backend="wine")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_profile_name("Fate/Grand Order")
        assert dialog.create_profile() is None
        assert dialog.error_label.text() == (
            "Use letters, numbers, spaces, - or _. A / or \\ is not allowed."
        )

        dialog.set_profile_name("mine")
        assert dialog.create_profile() is None
        assert dialog.error_label.text() == "That name is already used. Choose another name."
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_verify_is_called_check_game_files_and_failures_use_plain_words(
    tmp_path, qapp, monkeypatch
):
    view = GameView()
    try:
        assert view.verify.text() == "Check game files"
        assert view.verify.toolTip().startswith("Compares the installed game files")
    finally:
        view.deleteLater()
        qapp.processEvents()

    config = _config(tmp_path)
    config.add_profile("czn", backend="wine", title="Chaos Zero Nightmare")
    window = MainWindow(config)
    shown = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.information",
        lambda *a: shown.append(("info", a[1])),
    )
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.warning",
        lambda *a: shown.append(("warning", a[1])),
    )
    try:
        window._on_result("czn", {"verified": False, "message": "one file is wrong"})
        assert shown[-1] == ("warning", "File check failed")
        window._on_result("czn", {"verified": True, "message": "all good"})
        assert shown[-1] == ("info", "File check")
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_empty_library_and_cards_use_plain_language(qapp):
    page = LibraryPage()
    try:
        page.refresh({})
        assert "No games yet" in page.subtitle.text()
        assert "Chaos Zero Nightmare" in page.subtitle.text()
        assert "Arknights" in page.subtitle.text()
        assert "Stella Sora" in page.subtitle.text()
    finally:
        page.deleteLater()
        qapp.processEvents()

    add = AddGameCard()
    try:
        texts = [label.text() for label in add.findChildren(QLabel)]
        assert not any("backend" in text.lower() or "profile" in text.lower() for text in texts)
        assert "backend" not in add.toolTip().lower()
        assert "profile" not in add.toolTip().lower()
    finally:
        add.deleteLater()
        qapp.processEvents()

    card = GameCard("czn", "Chaos Zero Nightmare", "stove-czn")
    view = GameView()
    try:
        assert card.meta_label.text() == "Windows game via Proton"
        view.set_profile("ak", {"backend": "arknights-yostar", "title": "Arknights"})
        assert view.backend_label.text() == "Yostar launcher"
    finally:
        card.deleteLater()
        view.deleteLater()
        qapp.processEvents()
