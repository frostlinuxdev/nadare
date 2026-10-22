# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-game path uniqueness, import guards and honest install/update actions."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.backends.script import ScriptBackend
from nadare.backends.steam import SteamBackend
from nadare.core.backends import Status, supports_update
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_second_profile_gets_its_own_folder_and_prefix(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    data_root = tmp_path / "data"
    first = AddGameDialog(config, data_root=data_root)
    try:
        first.set_backend("arknights-yostar")
        first_folder = first.folder.text()
        first_prefix = first.prefix.text()
        assert first.create_profile() == "arknights"
    finally:
        first.deleteLater()
        qapp.processEvents()

    second = AddGameDialog(config, data_root=data_root)
    try:
        second.set_backend("arknights-yostar")
        second_folder = second.folder.text()
        second_prefix = second.prefix.text()
        assert second_folder != first_folder
        assert second_prefix != first_prefix
        assert second.create_profile() == "arknights-2"
    finally:
        second.deleteLater()
        qapp.processEvents()

    one = config.profile("arknights")
    two = config.profile("arknights-2")
    assert one["install_dir"] != two["install_dir"]
    assert one["prefix"] != two["prefix"]


def test_import_rejects_a_nonexistent_folder(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(tmp_path / "not-a-game")
        assert dialog.create_profile() is None
        assert config.profiles == {}
        assert dialog.error_label.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_rejects_a_folder_where_nothing_was_detected(tmp_path, qapp):
    empty = tmp_path / "empty"
    empty.mkdir()
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(empty)
        assert dialog.detect_label.text() == "Nothing detected yet."
        assert dialog.create_profile() is None
        assert config.profiles == {}
        assert "No Arknights installation" in dialog.error_label.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_generic_existing_folder_still_works(tmp_path, qapp):
    folder = tmp_path / "steamgame"
    folder.mkdir()
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("steam")
        dialog.set_mode(True)
        dialog.set_install_dir(folder)
        dialog.detect()
        assert dialog.detect_label.text() != "Nothing detected yet."
        assert dialog.create_profile() == "steam-game"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_script_and_steam_are_not_installable(tmp_path):
    script = ScriptBackend(make_context(tmp_path, "script", {}))
    steam = SteamBackend(make_context(tmp_path, "steam", {}))
    # Missing required keys are a configuration error, not a healthy "not installed".
    assert script.status().state == "error"
    assert script.status().data.get("can_install") is False
    assert steam.status().state == "error"
    assert steam.status().data.get("can_install") is False


def test_generic_backends_do_not_support_update():
    assert supports_update("wine") is False
    assert supports_update("script") is False
    assert supports_update("steam") is False
    assert supports_update("arknights-yostar") is True


def test_installed_generic_profile_disables_update(qapp):
    for kind in ("wine", "script", "steam"):
        view = GameView()
        view.set_profile("p", {"backend": kind, "title": "P"})
        view.set_status(Status("installed"))
        assert not view.update_btn.isEnabled(), kind
        assert view.update_btn.toolTip()


def test_installed_supported_profile_keeps_update_enabled(qapp):
    view = GameView()
    view.set_profile("p", {"backend": "arknights-yostar", "title": "P"})
    view.set_status(Status("installed"))
    assert view.update_btn.isEnabled()
