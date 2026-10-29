# SPDX-License-Identifier: GPL-3.0-or-later
"""The Add game wizard: install-new and import-existing profile creation."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog, detect_import


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _snapshot(root: Path) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        try:
            entries.append((str(path.relative_to(root)), path.stat().st_mtime_ns))
        except OSError:
            entries.append((str(path.relative_to(root)), -1))
    return entries


def _yostar_layout(root: Path, subdir: str, exe: str) -> Path:
    install = root / "drive_c" / "YostarGames" / subdir
    install.mkdir(parents=True)
    (install / exe).write_text("MZ")
    (install / "data.bin").write_bytes(b"payload")
    return install


def test_detect_import_finds_yostar_prefix_layout(tmp_path):
    prefix = tmp_path / "prefix"
    install = _yostar_layout(prefix, "Arknights_EN", "Arknights.exe")
    info = detect_import("arknights-yostar", prefix, None)
    assert info["prefix"] == str(prefix)
    assert info["install_dir"] == str(install)


def test_detect_import_finds_exe_in_picked_folder(tmp_path):
    folder = tmp_path / "Arknights"
    folder.mkdir()
    (folder / "Arknights.exe").write_text("MZ")
    info = detect_import("arknights-yostar", folder, tmp_path / "prefix")
    assert info["install_dir"] == str(folder)
    assert info["prefix"] == str(tmp_path / "prefix")


def test_import_existing_yostar_profile(tmp_path, qapp):
    prefix = tmp_path / "prefix"
    install = _yostar_layout(prefix, "Arknights_EN", "Arknights.exe")
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(prefix)
        dialog.set_prefix(prefix)
        created = dialog.create_profile()
        assert created == "arknights"
        profile = config.profile("arknights")
        assert profile["backend"] == "arknights-yostar"
        assert profile["title"] == "Arknights"
        assert profile["install_dir"] == str(install)
        assert profile["prefix"] == str(prefix)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_does_not_touch_the_picked_folders(tmp_path, qapp):
    prefix = tmp_path / "prefix"
    _yostar_layout(prefix, "StellaSora_EN", "StellaSora.exe")
    before = _snapshot(prefix)
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("stellasora-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(prefix)
        dialog.set_prefix(prefix)
        assert dialog.create_profile() == "stella-sora"
        assert _snapshot(prefix) == before
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_install_new_uses_defaults_under_the_data_root(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("wine")
        assert dialog.install_new.isChecked()
        folder = dialog.folder.text()
        assert folder.startswith(str(tmp_path / "data"))
        assert dialog.create_profile() == "wine-proton-game"
        profile = config.profile("wine-proton-game")
        assert profile["backend"] == "wine"
        assert profile["install_dir"] == folder
        assert profile["prefix"] == str(tmp_path / "data" / "wine-proton-game" / "prefix")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_profile_name_defaults_to_title_and_is_unique(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("arknights", backend="arknights-yostar")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        assert dialog.name.text() == "arknights-2"
        dialog.set_mode(True)
        existing = tmp_path / "somewhere"
        existing.mkdir()
        (existing / "Arknights.exe").write_text("MZ")
        dialog.set_install_dir(existing)
        assert dialog.create_profile() == "arknights-2"
        assert "arknights-2" in config.profiles
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_duplicate_explicit_name_is_rejected(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("mine", backend="wine")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("wine")
        dialog.set_profile_name("mine")
        assert dialog.create_profile() is None
        assert "already used" in dialog.error_label.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_non_wine_accepts_folder_as_is(tmp_path, qapp):
    folder = tmp_path / "steamgame"
    folder.mkdir()
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("steam")
        dialog.set_mode(True)
        dialog.set_install_dir(folder)
        assert dialog.create_profile() == "steam-game"
        assert config.profile("steam-game")["install_dir"] == str(folder)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_detect_import_prefix_only_kind_takes_folder_as_prefix(tmp_path):
    """G-5: for a prefix-only backend a folder that holds a prefix is the Wine prefix."""
    prefix = tmp_path / "czn-prefix"
    (prefix / "drive_c").mkdir(parents=True)
    info = detect_import("stove-czn", prefix, None)
    assert info["prefix"] == str(prefix)


def test_import_stove_czn_folder_becomes_the_prefix(tmp_path, qapp):
    """G-5: importing CZN from the install-folder field must keep the prefix."""
    prefix = tmp_path / "czn-prefix"
    (prefix / "drive_c").mkdir(parents=True)
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("stove-czn")
        dialog.set_mode(True)
        dialog.set_install_dir(prefix)  # the "Existing install folder" field
        created = dialog.create_profile()
        assert created == "chaos-zero-nightmare"
        profile = config.profile("chaos-zero-nightmare")
        assert profile["prefix"] == str(prefix)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_stove_czn_without_a_pick_is_rejected(tmp_path, qapp):
    """G-6: the auto-filled default prefix must not be accepted as an import."""
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("stove-czn")
        dialog.set_mode(True)
        assert dialog.prefix.text() == "", "the default prefix must be cleared on import"
        # Switching backend while already importing must not refill it either.
        dialog.set_backend("wine")
        dialog.set_backend("stove-czn")
        assert dialog.prefix.text() == ""
        assert dialog.create_profile() is None
        assert config.profiles == {}
        assert dialog.error_label.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_install_new_does_not_create_the_folder(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("wine")
        folder = Path(dialog.folder.text())
        dialog.create_profile()
        assert not folder.exists(), "the wizard must not create install folders"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_fgo_is_marked_experimental() -> None:
    from nadare.gui.add_game import CATALOG

    fgo = next(g for g in CATALOG if g.kind == "fgo-lepton")
    assert fgo.title == "Fate/Grand Order"  # the profile title stays clean
    assert fgo.badge == "Experimental"
    assert "binder" in fgo.badge_help


def test_only_an_import_is_marked_as_imported(tmp_path, qapp):
    prefix = tmp_path / "prefix"
    _yostar_layout(prefix, "StellaSora_EN", "StellaSora.exe")
    config = Config.load(tmp_path / "config.json")
    fresh = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        fresh.set_backend("arknights-yostar")
        assert fresh.create_profile() == "arknights"
        assert fresh.created_by_import is False  # nothing installed yet: no verify prompt
    finally:
        fresh.deleteLater()
    imported = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        imported.set_backend("stellasora-yostar")
        imported.set_mode(True)
        imported.set_install_dir(prefix)
        imported.set_prefix(prefix)
        assert imported.create_profile() == "stella-sora"
        assert imported.created_by_import is True
    finally:
        imported.deleteLater()
        qapp.processEvents()


def test_a_fresh_install_hides_folder_and_prefix_until_asked(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.show()
        qapp.processEvents()
        dialog.set_backend("arknights-yostar")
        assert not dialog.folder.isVisible() and not dialog.prefix.isVisible()
        assert dialog.location_toggle.isVisible()
        dialog.location_toggle.click()
        assert dialog.folder.isVisible()
        dialog.location_toggle.click()
        assert not dialog.folder.isVisible()
        dialog.set_mode(True)  # an import needs real paths
        assert dialog.folder.isVisible() and not dialog.location_toggle.isVisible()
        dialog.set_mode(False)
        result = dialog.create_profile()  # the hidden defaults still apply
        assert result == "arknights", (result, dialog.error_label.text())
    finally:
        dialog.close()
        qapp.processEvents()
