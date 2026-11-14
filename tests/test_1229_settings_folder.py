# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229 in Settings: where the game is, a locked game folder, a one-click reset of stray paths."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere

from nadare.core import flatpakfs, libraries
from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


@pytest.fixture
def rig(tmp_path, qapp, monkeypatch):
    state = {"value": "installed"}
    patch_everywhere(monkeypatch, settings_mod, "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state=state["value"], data={})
        ),
    )
    shown: list[str] = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: shown.append(a[2]))
    config = Config.load(tmp_path / "config.json")
    page = SettingsPage()
    rig = SimpleNamespace(config=config, page=page, shown=shown, state=state, tmp=tmp_path)
    yield rig
    page.deleteLater()
    qapp.processEvents()


def _library_game(rig, lib: Path) -> Path:
    libraries.add_library(rig.config, lib)
    game = lib / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / "prefix" / "drive_c").mkdir(parents=True)
    rig.config.add_profile(
        "bd2",
        backend="direct-bd2",
        title="Brown Dust 2",
        install_dir=str(game / "install"),
        prefix=str(game / "prefix"),
        owned_dirs=[str(game)],
        owned_trees=["install_dir", "prefix"],
    )
    libraries.write_manifest(rig.config.profiles["bd2"], game)
    rig.config.save()
    return game


def _load(rig, qapp, name="bd2"):
    rig.page.load(rig.config, name)
    _settle(qapp)


def test_an_installed_games_folder_is_locked_like_its_prefix(rig, qapp, tmp_path):
    _library_game(rig, tmp_path / "Games")
    _load(rig, qapp)
    assert rig.page.install_dir.isReadOnly()
    assert not rig.page._install_dir_browse.isEnabled()


def test_a_game_not_installed_changes_folder_only_by_change_location(rig, qapp, tmp_path):
    """#1293: free text let install and prefix drift apart; see test_1293_settings_location."""
    _library_game(rig, tmp_path / "Games")
    rig.state["value"] = "not_installed"
    _load(rig, qapp)
    assert rig.page.install_dir.isReadOnly()
    assert not rig.page._install_dir_browse.isEnabled()
    assert not rig.page.change_location_button.isHidden()


def test_a_library_game_says_which_library(rig, qapp, tmp_path):
    lib = tmp_path / "Games"
    game = _library_game(rig, lib)
    _load(rig, qapp)
    text = rig.page.place_label.text()
    assert not rig.page.place_label.isHidden()
    assert str(game.resolve()) in text
    assert "library" in text.lower() and "outside" not in text.lower()
    assert rig.page.reset_stray_button.isHidden()


def test_an_import_says_outside_your_libraries(rig, qapp, tmp_path):
    where = tmp_path / "old" / "BD2"
    where.mkdir(parents=True)
    rig.config.add_profile(
        "bd2", backend="direct-bd2", title="Brown Dust 2", install_dir=str(where)
    )
    _load(rig, qapp)
    text = rig.page.place_label.text()
    assert "Outside your libraries" in text
    assert "never deletes" in text


def test_a_stray_path_is_shown_and_reset_in_one_click(rig, qapp, tmp_path):
    game = _library_game(rig, tmp_path / "Games")
    rig.config.profiles["bd2"]["install_dir"] = str(tmp_path / "src")
    rig.config.save()
    _load(rig, qapp)
    assert not rig.page.reset_stray_button.isHidden()
    assert "outside the game folder" in rig.page.place_label.text()
    rig.page.reset_stray_button.click()
    _settle(qapp)
    saved = Config.load(rig.config.path).profiles["bd2"]
    assert saved["install_dir"] == str((game / "install").resolve())
    assert rig.page.reset_stray_button.isHidden()
    assert rig.page.install_dir.text() == str((game / "install").resolve())


def test_custom_wine_games_show_no_placement(rig, qapp, tmp_path):
    rig.config.add_profile("w", backend="wine", title="W", install_dir=str(tmp_path / "w"))
    _load(rig, qapp, "w")
    assert rig.page.place_label.isHidden()
    assert rig.page.reset_stray_button.isHidden()


def test_saving_a_folder_the_flatpak_cannot_reach_is_refused(rig, qapp, tmp_path, monkeypatch):
    # A custom Wine game: catalog games have no free folder field any more (#1293).
    rig.config.add_profile(
        "bd2", backend="wine", title="Mine", exe_name="game.exe",
        install_dir=str(tmp_path / "mine"), prefix=str(tmp_path / "pfx"),
    )
    rig.config.save()
    rig.state["value"] = "not_installed"
    _load(rig, qapp)
    target = tmp_path / "unreachable"
    monkeypatch.setattr(
        flatpakfs,
        "unreachable_reason",
        lambda folder: "Nadare cannot reach that folder" if str(target) in str(folder) else None,
    )
    rig.page.install_dir.setText(str(target))
    rig.page.save()
    assert any("cannot reach" in text for text in rig.shown)
    assert Config.load(rig.config.path).profiles["bd2"]["install_dir"] != str(target)
