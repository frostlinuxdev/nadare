# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings keeps unsaved edits through navigation; a failed save rolls everything back (#852)."""

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


@pytest.fixture
def asked(monkeypatch):
    """Record every Discard question and answer it with ``asked.answer``."""

    class Asked(list):
        answer = QMessageBox.StandardButton.No

    log = Asked()

    def question(parent, title, text, *args, **kwargs):
        log.append(title)
        return log.answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return log


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.add_profile("a", backend="wine", title="Original")
    config.add_profile("b", backend="wine", title="Other")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    return config, window


def _edit_settings(window):
    window.open_game("a")
    window.open_settings()
    page = window.settings
    page.title.setText("Edited")
    assert page._has_unsaved_changes()
    return page


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_clicking_settings_again_keeps_the_edit(tmp_path, qapp, asked):
    _config, window = _window(tmp_path)
    try:
        page = _edit_settings(window)
        window.settings_button.click()
        assert page.title.text() == "Edited"
        assert window.stack.currentIndex() == PAGE_SETTINGS
    finally:
        _close(window, qapp)


@pytest.mark.parametrize("button", ["library_button", "store_button", "downloads_button"])
def test_leaving_by_the_sidebar_asks_first_and_no_stays(tmp_path, qapp, asked, button):
    _config, window = _window(tmp_path)
    try:
        page = _edit_settings(window)
        getattr(window, button).click()
        assert asked == ["Discard changes"]
        assert window.stack.currentIndex() == PAGE_SETTINGS
        assert page.title.text() == "Edited"
    finally:
        _close(window, qapp)


def test_answering_yes_leaves_and_discards(tmp_path, qapp, asked):
    _config, window = _window(tmp_path)
    try:
        asked.answer = QMessageBox.StandardButton.Yes
        _edit_settings(window)
        window.library_button.click()
        assert asked == ["Discard changes"]
        assert window.stack.currentIndex() != PAGE_SETTINGS
    finally:
        _close(window, qapp)


def test_opening_another_game_from_the_sidebar_asks_too(tmp_path, qapp, asked):
    _config, window = _window(tmp_path)
    try:
        _edit_settings(window)
        window.sidebar_games.open_game.emit("b")
        assert asked == ["Discard changes"]
        assert window.stack.currentIndex() == PAGE_SETTINGS
    finally:
        _close(window, qapp)


def test_leaving_without_edits_never_asks(tmp_path, qapp, asked):
    _config, window = _window(tmp_path)
    try:
        window.open_game("a")
        window.open_settings()
        window.library_button.click()
        assert asked == []
        assert window.stack.currentIndex() != PAGE_SETTINGS
    finally:
        _close(window, qapp)


def test_back_asks_once_not_twice(tmp_path, qapp, asked):
    _config, window = _window(tmp_path)
    try:
        asked.answer = QMessageBox.StandardButton.Yes
        page = _edit_settings(window)
        page._request_back()
        assert asked == ["Discard changes"]
        assert window.stack.currentIndex() != PAGE_SETTINGS
    finally:
        _close(window, qapp)


def test_a_failed_save_puts_every_global_setting_back(tmp_path, qapp, monkeypatch):
    config, window = _window(tmp_path)
    try:
        config.data["default_monitor"] = "DP-1"
        window.open_game("a")
        window.open_settings()
        page = window.settings
        page.default_monitor.addItem("HDMI-9", "HDMI-9")
        page.default_monitor.setCurrentIndex(page.default_monitor.findData("HDMI-9"))
        page.check_updates.setChecked(not page.check_updates.isChecked())
        before = {k: config.data.get(k) for k in ("default_monitor", "check_updates")}
        monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))

        def broken(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(config, "save", broken)
        page.save()
        assert {k: config.data.get(k) for k in before} == before
    finally:
        _close(window, qapp)
