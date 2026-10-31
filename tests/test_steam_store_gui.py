# SPDX-License-Identifier: GPL-3.0-or-later
"""Install on a Steam Store tile: one confirm, a plain steam profile, then the Set up window."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController

EPIC = "5019180"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _window(tmp_path: Path, monkeypatch, profiles: dict | None = None) -> MainWindow:
    monkeypatch.setattr(BackendController, "_spawn", lambda self, *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    for name, fields in (profiles or {}).items():
        config.add_profile(name, **fields)
    config.save()
    return MainWindow(config)


def _close(window: MainWindow, qapp) -> None:
    window.controller._workers.clear()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_install_on_a_tile_makes_a_steam_profile_and_opens_set_up(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    asked: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(window, "_ask_steam_tile", lambda title: asked.append(title) or True)
    monkeypatch.setattr(window, "_open_steam_setup", lambda: opened.append(window._current_profile))
    try:
        window._store_install(f"steam-{EPIC}")
        assert asked == ["Epic Seven"]
        data = window.config.profiles["Epic Seven"]
        assert data["backend"] == "steam" and data["steam_app_id"] == EPIC
        assert opened == ["Epic Seven"]
        assert f"steam-{EPIC}" in window.store._installed  # the tile now says installed
        assert Config.load(tmp_path / "config.json").profiles["Epic Seven"]["steam_app_id"] == EPIC
    finally:
        _close(window, qapp)


def test_declining_the_confirm_changes_nothing(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(window, "_ask_steam_tile", lambda title: False)
    monkeypatch.setattr(window, "_open_steam_setup", lambda: pytest.fail("must not open"))
    try:
        window._store_install(f"steam-{EPIC}")
        assert window.config.profiles == {}
    finally:
        _close(window, qapp)


def test_a_game_already_added_is_opened_not_duplicated(tmp_path, qapp, monkeypatch):
    mine = {"backend": "steam", "title": "My E7", "steam_app_id": EPIC}
    window = _window(tmp_path, monkeypatch, {"my e7": mine})
    opened: list[str] = []
    monkeypatch.setattr(window, "_ask_steam_tile", lambda title: pytest.fail("no confirm"))
    monkeypatch.setattr(window, "_open_steam_setup", lambda: opened.append(window._current_profile))
    try:
        window._store_install(f"steam-{EPIC}")
        assert list(window.config.profiles) == ["my e7"]
        assert opened == ["my e7"]
    finally:
        _close(window, qapp)


def test_a_taken_name_gets_a_number(tmp_path, qapp, monkeypatch):
    taken = {"backend": "wine", "title": "x"}
    window = _window(tmp_path, monkeypatch, {"Epic Seven": taken})
    monkeypatch.setattr(window, "_ask_steam_tile", lambda title: True)
    monkeypatch.setattr(window, "_open_steam_setup", lambda: None)
    try:
        window._store_install(f"steam-{EPIC}")
        assert window.config.profiles["Epic Seven 2"]["steam_app_id"] == EPIC
        assert window.config.profiles["Epic Seven"]["backend"] == "wine"
    finally:
        _close(window, qapp)


def test_an_unknown_steam_kind_is_ignored(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(window, "_ask_steam_tile", lambda title: pytest.fail("no confirm"))
    try:
        window._store_install("steam-999")
        assert window.config.profiles == {}
    finally:
        _close(window, qapp)


def test_play_on_a_steam_tile_opens_the_matching_game_not_another_steam_one(
    tmp_path, qapp, monkeypatch
):
    profiles = {
        "other": {"backend": "steam", "title": "Other", "steam_app_id": "3224770"},
        "e7": {"backend": "steam", "title": "E7", "steam_app_id": EPIC},
    }
    window = _window(tmp_path, monkeypatch, profiles)
    opened: list[str] = []
    monkeypatch.setattr(window, "open_game", opened.append)
    try:
        window._store_play(f"steam-{EPIC}")
        assert opened == ["e7"]
    finally:
        _close(window, qapp)
