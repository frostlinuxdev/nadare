# SPDX-License-Identifier: GPL-3.0-or-later
"""#765: a game that is already installed offers Play on its Store page."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.config import Config
from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.main_window import MainWindow
from nadare.gui.store_detail import StoreDetail


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _entry(kind: str, **changes) -> StoreEntry:
    base = next(item for item in entries() if item.kind == kind)
    values = {field: getattr(base, field) for field in StoreEntry.__dataclass_fields__}
    values.update(changes)
    return StoreEntry(**values)


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


def test_an_installed_game_shows_play_as_the_main_button(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=True))
    assert not widget.play_button.isHidden()
    assert widget.play_button.text() == "Play"
    assert widget.play_button.objectName() == "Primary"
    # Installing another copy is still possible, but it is no longer the main action.
    assert widget.install_button.text() == "Add another copy"
    assert widget.install_button.objectName() != "Primary"


def test_a_game_that_is_not_installed_has_no_play_button(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=False))
    assert widget.play_button.isHidden()
    assert widget.install_button.text() == "Install"
    assert widget.install_button.objectName() == "Primary"


def test_play_button_tracks_the_installed_state_and_emits_the_kind(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=False))
    received = []
    widget.play_requested.connect(received.append)
    widget.set_entry(_entry("direct-pgr", installed=True))
    assert not widget.play_button.isHidden()
    widget.play_button.click()
    assert received == ["direct-pgr"]
    widget.set_entry(_entry("direct-pgr", installed=False))
    assert widget.play_button.isHidden()
    assert widget.install_button.objectName() == "Primary"


def test_the_initial_focus_goes_to_play_for_an_installed_game(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=True))
    widget.show()
    qapp.processEvents()
    assert widget.focusWidget() is widget.play_button
    widget.close()


def test_play_in_the_store_opens_that_games_page(tmp_path, qapp):
    window = _window(
        tmp_path,
        other={"backend": "launcher-nikke", "title": "NIKKE"},
        pgr={"backend": "direct-pgr", "title": "PGR"},
    )
    opened = []
    window.open_game = opened.append
    try:
        window.show_store()
        window.store.show_detail("direct-pgr")
        assert not window.store.detail.play_button.isHidden()
        window.store.detail.play_button.click()
        assert opened == ["pgr"]
    finally:
        _close(window, qapp)


def test_play_with_two_copies_opens_the_first_and_does_not_start_an_install(tmp_path, qapp):
    window = _window(
        tmp_path,
        first={"backend": "direct-pgr", "title": "PGR"},
        second={"backend": "direct-pgr", "title": "PGR 2"},
    )
    opened = []
    window.open_game = opened.append
    started = []
    window._start = lambda *a, **k: started.append(a) or True
    try:
        window.show_store()
        window.store.show_detail("direct-pgr")
        window.store.detail.play_button.click()
        assert opened == ["first"] and started == []
    finally:
        _close(window, qapp)


def test_play_for_a_game_whose_profile_vanished_does_not_crash(tmp_path, qapp):
    window = _window(tmp_path)
    opened = []
    window.open_game = opened.append
    try:
        window._store_play("direct-pgr")  # no profile for this kind any more
        assert opened == []
    finally:
        _close(window, qapp)
