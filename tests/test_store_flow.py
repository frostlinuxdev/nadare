# SPDX-License-Identifier: GPL-3.0-or-later
"""The store wired into the main window: navigation, install hand-off, art and the dialog modes."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QThreadPool
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QDialog

from nadare.core import artwork
from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui import main_window as main_window_mod
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


def _settle(flow: StoreFlow, qapp, timeout: float = 5.0) -> None:
    """Wait (in real time) for background fetches and decodes and the callbacks they queue."""
    pool = QThreadPool.globalInstance()
    deadline = time.monotonic() + timeout
    quiet = 0
    while time.monotonic() < deadline:
        idle = pool.waitForDone(50)
        QCoreApplication.processEvents()
        # Idle pool and nothing pending, twice in a row: a finished fetch can still have
        # queued the decode request that only the next pass delivers.
        quiet = quiet + 1 if idle and not flow._images.pending() else 0
        if quiet >= 2:
            return
    raise AssertionError("background art work did not settle")


def _red_image(path: Path, colour: str = "#ff0000") -> Path:
    image = QImage(160, 90, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    assert image.save(str(path))
    return path


def _hero_colour(flow: StoreFlow) -> QColor:
    image = flow.detail.hero_label.pixmap().toImage()
    return QColor(image.pixel(image.width() // 2, image.height() // 2))


def _is(colour: QColor, name: str) -> bool:
    target = QColor(name)
    got, want = colour.getRgb()[:3], target.getRgb()[:3]
    return all(abs(a - b) < 40 for a, b in zip(got, want, strict=True))


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


def test_library_add_game_opens_the_store_not_the_dialog(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    built = []
    monkeypatch.setattr(main_window_mod, "AddGameDialog", lambda *a, **kw: built.append(kw))
    try:
        window.library.add_game.emit()
        assert window.stack.currentIndex() == PAGE_STORE
        assert built == []
    finally:
        _close(window, qapp)


def test_detail_back_restores_the_grid_with_its_search_and_filter(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.show_store()
        window.show()
        qapp.processEvents()
        grid = window.store.grid
        grid.search.setText("a")
        grid.tier_filter.setCurrentText("Verified")
        before = grid.visible_kinds()
        assert "direct-pgr" in before
        grid.entry_activated.emit("direct-pgr")
        assert window.store.currentWidget() is window.store.detail
        assert window.store.detail.title_label.text() == "Punishing: Gray Raven"
        window.store.detail.back_button.click()
        assert window.store.currentWidget() is grid
        assert grid.search.text() == "a"
        assert grid.tier_filter.currentText() == "Verified"
        assert grid.visible_kinds() == before
        grid.entry_activated.emit("direct-pgr")
        window.store.show_grid()
        assert window.store.currentWidget() is grid
    finally:
        _close(window, qapp)


def test_opening_the_store_from_the_sidebar_returns_to_the_grid(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.show_store()
        window.store.show_detail("direct-pgr")
        window.show_library()
        window.show_store()
        assert window.store.currentWidget() is window.store.grid
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
        self.created_by_import = False
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
    monkeypatch.setattr(main_window_mod, "AddGameDialog", _FakeDialog)
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
        window.store.show_detail("direct-pgr")
        window.store.detail.install_button.click()
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
        window.store.show_detail("direct-pgr")
        assert window.store.detail.install_button.text() == "Install"
        window.store.detail.install_button.click()
        (name,) = window.config.profiles
        assert opened == [name] and started == [(name, "install")]
        assert not window.store.grid._cards["direct-pgr"].added_marker.isHidden()
        assert window.store.detail.install_button.text() == "Add another"
        assert not window.store.detail.installed_note.isHidden()
    finally:
        _close(window, qapp)


def test_add_your_own_game_opens_the_advanced_dialog(tmp_path, qapp, fake_dialog):
    window = _window(tmp_path)
    try:
        window.show_store()
        window.store.grid.own_game_button.click()
        assert [d.kwargs for d in fake_dialog.instances] == [{"kind": None, "advanced": True}]
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
def test_hero_for_the_shown_game_replaces_the_placeholder(tmp_path, qapp, monkeypatch):
    red = _red_image(tmp_path / "red.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": red)
    flow = StoreFlow()
    try:
        flow.show_detail("direct-pgr")
        assert not _is(_hero_colour(flow), "#ff0000")
        _settle(flow, qapp)
        assert _is(_hero_colour(flow), "#ff0000")
    finally:
        flow.deleteLater()
        qapp.processEvents()


def test_a_hero_for_a_game_the_user_left_is_ignored(tmp_path, qapp, monkeypatch):
    colours = {"direct-pgr": _red_image(tmp_path / "a.png", "#ff0000"),
               "direct-nte": _red_image(tmp_path / "b.png", "#0000ff")}
    monkeypatch.setattr(
        artwork, "fetch", lambda profile, title=None, kind="cover": colours[profile["backend"]]
    )
    flow = StoreFlow()
    try:
        flow.show_detail("direct-pgr")
        flow.show_detail("direct-nte")  # the first fetch is now stale
        _settle(flow, qapp)
        assert flow.detail.title_label.text() != "Punishing: Gray Raven"
        assert _is(_hero_colour(flow), "#0000ff")
        # Leaving the page must also drop a fetch that is still in flight: the detail keeps
        # its placeholder instead of receiving art for a game the user already left.
        flow.show_detail("direct-pgr")
        flow.show_grid()
        _settle(flow, qapp)
        assert not _is(_hero_colour(flow), "#ff0000")
        assert not _is(_hero_colour(flow), "#0000ff")
    finally:
        flow.deleteLater()
        qapp.processEvents()


def test_unrelated_profile_changes_do_not_reset_the_hero(tmp_path, qapp, monkeypatch):
    red = _red_image(tmp_path / "red.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": red)
    flow = StoreFlow()
    try:
        flow.show_detail("direct-pgr")
        _settle(flow, qapp)
        assert _is(_hero_colour(flow), "#ff0000")
        flow.set_installed_kinds(frozenset({"direct-nte"}))
        assert _is(_hero_colour(flow), "#ff0000")
        assert flow.detail.install_button.text() == "Install"
        flow.set_installed_kinds(frozenset({"direct-pgr"}))
        assert flow.detail.install_button.text() == "Add another"
    finally:
        flow.deleteLater()
        qapp.processEvents()


def test_games_without_art_keep_the_placeholder_and_never_fetch(qapp, monkeypatch):
    from nadare.core import storecatalog

    calls = []
    monkeypatch.setattr(artwork, "fetch", lambda *a, **kw: calls.append(a) or None)
    monkeypatch.setattr(storecatalog, "publisher_art_kinds", lambda: frozenset())
    flow = StoreFlow()
    try:
        flow.show_detail("stove-czn")  # no art source once the publisher kinds are gone
        _settle(flow, qapp)
        assert calls == []
        assert not flow.detail.hero_label.pixmap().isNull()
    finally:
        flow.deleteLater()
        qapp.processEvents()


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
        window.store.show_detail("direct-pgr")
        qapp.processEvents()
        detail = window.store.detail
        assert detail.layout().minimumSize().width() <= window.store.width()
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


def test_advanced_dialog_lists_only_the_three_advanced_types(tmp_path, qapp):
    dialog = AddGameDialog(_config(tmp_path), advanced=True)
    try:
        advanced = {
            g.kind for g in add_game.available_catalog() if g.group == add_game.ADVANCED_GROUP
        }
        assert advanced == {"wine", "steam", "script"}
        assert dialog.game_list.count() == 1 + len(advanced)
        assert dialog.game_list.item(0).text().startswith("ADVANCED")
        assert dialog.selected_kind() in advanced
        assert not dialog.game_list.isHidden()
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
def test_hero_fetches_report_through_the_flows_own_signals(tmp_path, qapp, monkeypatch):
    from nadare.gui import store_flow

    red = _red_image(tmp_path / "red.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": red)
    flow = StoreFlow()
    try:
        flow.show_detail("direct-pgr")
        generation = flow._generation
        task = store_flow._FetchHero(generation, "direct-pgr", "Punishing", flow._hero_signals)
        assert task.signals is flow._hero_signals
        task.run()
        _settle(flow, qapp)
        assert _is(_hero_colour(flow), "#ff0000")
    finally:
        flow.deleteLater()
        qapp.processEvents()


def test_a_hero_fetch_finishing_after_the_flow_died_does_not_raise(tmp_path, qapp, monkeypatch):
    from PyQt6 import sip

    from nadare.gui import store_flow

    red = _red_image(tmp_path / "red.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": red)
    flow = StoreFlow()
    task = store_flow._FetchHero(0, "direct-pgr", "Punishing", flow._hero_signals)
    sip.delete(flow)
    task.run()
