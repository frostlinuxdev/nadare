# SPDX-License-Identifier: GPL-3.0-or-later
"""One profile per game (user decision 2026-11-07, after b21).

Two profiles of one game shared prefixes, accounts and saves in ways that caused several b21 bugs
(#956-#962, #971, #972). A catalog game can now be added once; Add game and the Store point to
the existing entry instead. Games the user adds from their own .exe, scripts and Steam games
stay unlimited: each of those profiles is a different game. Existing duplicates are left alone.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import backends
from nadare.core.config import Config
from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.add_game import AddGameDialog
from nadare.gui.main_window import MainWindow
from nadare.gui.store_detail import StoreDetail


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---- the rule ----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["direct-pgr", "launcher-nikke", "arknights-yostar", "stove-czn"])
def test_catalog_games_are_one_per_library(kind):
    assert backends.one_per_game(kind) is True


@pytest.mark.parametrize("kind", ["wine", "script", "steam", "", "no-such-kind"])
def test_games_the_user_brings_are_unlimited(kind):
    assert backends.one_per_game(kind) is False


def test_existing_profile_finds_the_first_one_of_that_game():
    profiles = {
        "a": {"backend": "wine"},
        "pgr": {"backend": "direct-pgr"},
        "pgr-2": {"backend": "direct-pgr"},
    }
    assert backends.existing_profile("direct-pgr", profiles) == "pgr"
    assert backends.existing_profile("launcher-nikke", profiles) is None
    assert backends.existing_profile("wine", profiles) is None  # unlimited kinds never match


# ---- Add game ------------------------------------------------------------------------------
def _config(tmp_path, **profiles) -> Config:
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles.items():
        config.add_profile(name, **values)
    config.save()
    return config


def test_add_game_refuses_a_second_copy_and_names_the_first(tmp_path, qapp):
    config = _config(tmp_path, pgr={"backend": "direct-pgr", "title": "Punishing: Gray Raven"})
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("direct-pgr")
        assert dialog.create_profile() is None
        message = dialog.error_label.text()
        assert "already in your library" in message and "Punishing: Gray Raven" in message
        assert set(config.profiles) == {"pgr"}
        assert set(Config.load(tmp_path / "config.json").profiles) == {"pgr"}
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_add_game_refuses_an_import_of_a_game_already_there(tmp_path, qapp):
    prefix = tmp_path / "prefix"
    install = prefix / "drive_c" / "YostarGames" / "Arknights_EN"
    install.mkdir(parents=True)
    (install / "Arknights.exe").write_text("MZ")
    config = _config(tmp_path, arknights={"backend": "arknights-yostar", "title": "Arknights"})
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(prefix)
        dialog.set_prefix(prefix)
        assert dialog.create_profile() is None
        assert set(config.profiles) == {"arknights"}
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_add_game_still_adds_a_second_wine_game(tmp_path, qapp):
    exe = tmp_path / "two" / "two.exe"
    exe.parent.mkdir()
    exe.write_text("MZ")
    config = _config(tmp_path, one={"backend": "wine", "title": "One"})
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("wine")
        dialog.name.setText("two")
        created = dialog.create_profile()
        assert created and created != "one"
        assert len(config.profiles) == 2
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_existing_duplicates_are_left_alone(tmp_path, qapp):
    config = _config(
        tmp_path,
        pgr={"backend": "direct-pgr", "title": "PGR"},
        **{"pgr-2": {"backend": "direct-pgr", "title": "PGR"}},
    )
    window = MainWindow(config)
    try:
        assert set(window.config.profiles) == {"pgr", "pgr-2"}
        assert set(Config.load(tmp_path / "config.json").profiles) == {"pgr", "pgr-2"}
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- the Store -----------------------------------------------------------------------------
def _entry(kind: str, **changes) -> StoreEntry:
    base = next(item for item in entries() if item.kind == kind)
    values = {field: getattr(base, field) for field in StoreEntry.__dataclass_fields__}
    values.update(changes)
    return StoreEntry(**values)


def test_an_installed_game_offers_play_and_no_second_copy(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=True))
    try:
        assert not widget.play_button.isHidden()
        assert widget.install_button.isHidden()
    finally:
        widget.deleteLater()


def test_the_install_button_comes_back_when_the_game_is_removed(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=True))
    try:
        widget.set_entry(_entry("direct-pgr", installed=False))
        assert not widget.install_button.isHidden()
        assert widget.install_button.text() == "Install"
    finally:
        widget.deleteLater()


def test_store_install_of_a_game_already_there_opens_it(tmp_path, qapp):
    config = _config(tmp_path, pgr={"backend": "direct-pgr", "title": "PGR"})
    window = MainWindow(config)
    opened: list[str] = []
    started: list[tuple] = []
    window.open_game = opened.append
    window._start = lambda *a, **k: started.append(a) or True
    try:
        window._store_install("direct-pgr")
        assert opened == ["pgr"] and started == []
        assert set(window.config.profiles) == {"pgr"}
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
