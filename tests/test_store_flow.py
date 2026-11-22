# SPDX-License-Identifier: GPL-3.0-or-later
"""The store wired into the main window: navigation, install hand-off, art and the dialog modes."""

from __future__ import annotations

import os
import socket
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _main_window_patch import patch_main_window
from PyQt6.QtWidgets import QApplication, QDialog

from nadare.core import artwork
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog
from nadare.gui.main_window import PAGE_GAME, PAGE_LIBRARY, PAGE_SETTINGS, PAGE_STORE, MainWindow
from nadare.gui.store_flow import StoreFlow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _window(tmp_path: Path, **profiles: dict) -> MainWindow:
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles.items():
        config.add_profile(name, **values)
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()


# ---- navigation ------------------------------------------------------------
def test_old_page_indexes_do_not_move(qapp):
    assert (PAGE_LIBRARY, PAGE_GAME, PAGE_SETTINGS) == (0, 1, 2)
    assert PAGE_STORE == 3


def test_store_button_is_the_first_nav_item_and_follows_the_page(tmp_path, qapp):
    window = _window(tmp_path, sample={"backend": "wine", "title": "Sample"})
    try:
        nav = window.store_button.parentWidget().layout()
        assert nav.indexOf(window.store_button) < nav.indexOf(window.library_button)
        assert nav.indexOf(window.library_button) < nav.indexOf(window.settings_button)
        window.store_button.click()
        assert window.stack.currentIndex() == PAGE_STORE
        assert window.store_button.property("active") == "true"
        assert window.library_button.property("active") == "false"
        window.library_button.click()
        assert window.stack.currentIndex() == PAGE_LIBRARY
        assert window.store_button.property("active") == "false"
        assert window.library_button.property("active") == "true"
        window.show_store()
        window.open_game("sample")
        assert window.stack.currentIndex() == PAGE_GAME
        assert window.store_button.property("active") == "false"
    finally:
        _close(window, qapp)


def test_first_start_with_no_games_opens_the_store(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        assert window.stack.currentIndex() == PAGE_STORE
        assert window.store_button.property("active") == "true"
    finally:
        _close(window, qapp)


def test_library_add_game_opens_the_store_not_the_dialog(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    built = []
    patch_main_window(monkeypatch, "AddGameDialog", lambda *a, **kw: built.append(kw))
    try:
        window.library.add_game.emit()
        assert window.stack.currentIndex() == PAGE_STORE
        assert built == []
    finally:
        _close(window, qapp)


# ---- install hand-off ------------------------------------------------------
class _FakeDialog:
    """Stands in for AddGameDialog: records how it was built and can 'create' a profile."""

    instances: list = []
    outcome: str = "reject"

    def __init__(self, config, parent=None, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.created_profile = ""
        type(self).instances.append(self)

    def exec(self):
        if type(self).outcome == "accept":
            self.config.add_profile("pgr", backend=self.kwargs["kind"], title="Punishing")
            self.created_profile = "pgr"
            return QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Rejected


@pytest.fixture
def fake_dialog(monkeypatch):
    _FakeDialog.instances = []
    _FakeDialog.outcome = "reject"
    patch_main_window(monkeypatch, "AddGameDialog", _FakeDialog)
    return _FakeDialog


@pytest.fixture
def quick_dialog(monkeypatch):
    """Replace the one-step confirm's exec; ``outcome`` decides what the user pressed."""
    from nadare.gui.quick_install import QuickInstallDialog

    class State:
        shown: list = []
        outcome = "reject"

    def fake_exec(self):
        State.shown.append(self)
        accepted = State.outcome == "accept"
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    State.shown = []
    State.outcome = "reject"
    monkeypatch.setattr(QuickInstallDialog, "exec", fake_exec)
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    return State


def test_install_asks_once_for_that_game_and_rejecting_changes_nothing(
    tmp_path, qapp, quick_dialog
):
    window = _window(tmp_path)
    opened = []
    window.open_game = opened.append
    try:
        window.show_store()
        window.store.grid.entry_activated.emit("direct-pgr")  # the card opens the dialog (#1536)
        assert len(quick_dialog.shown) == 1
        assert window.config.profiles == {}
        assert opened == []
    finally:
        _close(window, qapp)


def test_accepted_install_creates_the_profile_marks_it_added_and_opens_it(
    tmp_path, qapp, quick_dialog, monkeypatch
):
    window = _window(tmp_path)
    opened = []
    window.open_game = opened.append
    started = []
    monkeypatch.setattr(window, "_start", lambda p, op, *a, **k: started.append((p, op)) or True)
    quick_dialog.outcome = "accept"
    try:
        window.show_store()
        window.show()
        qapp.processEvents()
        card = window.store.grid._cards["direct-pgr"]
        assert card.added_marker.isHidden()
        window.store.grid.entry_activated.emit("direct-pgr")  # the card opens the dialog (#1536)
        (name,) = window.config.profiles
        assert opened == [name] and started == [(name, "install")]
        assert not window.store.grid._cards["direct-pgr"].added_marker.isHidden()
    finally:
        _close(window, qapp)


def test_existing_profiles_show_as_added_in_a_fresh_store(tmp_path, qapp):
    window = _window(tmp_path, owned={"backend": "direct-pgr", "title": "PGR"})
    try:
        window.show_store()
        window.show()
        qapp.processEvents()
        assert not window.store.grid._cards["direct-pgr"].added_marker.isHidden()
        assert window.store.grid._cards["direct-nte"].added_marker.isHidden()
    finally:
        _close(window, qapp)


# ---- hero art ---------------------------------------------------------------
# ---- layout and safety -----------------------------------------------------
@pytest.mark.parametrize("width", [900, 1400])
def test_store_has_no_horizontal_overflow_in_the_window(tmp_path, qapp, width):
    window = _window(tmp_path)
    try:
        window.resize(width, 720)
        window.show_store()
        window.show()
        qapp.processEvents()
        grid = window.store.grid
        assert grid.scroll.horizontalScrollBar().maximum() == 0
        assert window.store.width() <= width
        assert grid.layout().minimumSize().width() <= window.store.width()
    finally:
        _close(window, qapp)


def test_building_the_flow_touches_no_network(monkeypatch, qapp):
    def forbidden(*args, **kwargs):
        raise AssertionError("the store flow opened a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    flow = StoreFlow()
    flow.deleteLater()
    qapp.processEvents()


# ---- AddGameDialog modes ---------------------------------------------------
def _config(tmp_path: Path) -> Config:
    return Config.load(tmp_path / "config.json")


def test_dialog_with_a_kind_hides_the_game_list_and_preselects_the_game(tmp_path, qapp):
    dialog = AddGameDialog(_config(tmp_path), kind="direct-pgr")
    plain = AddGameDialog(_config(tmp_path))
    try:
        assert dialog.game_list.isHidden()
        assert not plain.game_list.isHidden()
        assert dialog.selected_kind() == "direct-pgr"
        assert dialog.windowTitle() == "Add game"
    finally:
        dialog.deleteLater()
        plain.deleteLater()


def test_dialog_with_a_kind_still_creates_that_games_profile(tmp_path, qapp):
    config = _config(tmp_path)
    dialog = AddGameDialog(config, data_root=tmp_path / "data", kind="direct-pgr")
    try:
        dialog.set_install_dir(tmp_path / "games" / "pgr")
        name = dialog.create_profile()
        assert name and config.profiles[name]["backend"] == "direct-pgr"
    finally:
        dialog.deleteLater()


def test_unknown_kind_falls_back_to_the_normal_dialog(tmp_path, qapp):
    plain = AddGameDialog(_config(tmp_path))
    unknown = AddGameDialog(_config(tmp_path), kind="not-a-game")
    try:
        assert not unknown.game_list.isHidden()
        assert unknown.game_list.count() == plain.game_list.count()
        assert unknown.selected_kind() == plain.selected_kind()
    finally:
        plain.deleteLater()
        unknown.deleteLater()


# ---- pool tasks must not own a QObject (see test_qrunnable_signal_ownership.py) --------
