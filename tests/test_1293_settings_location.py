# SPDX-License-Identifier: GPL-3.0-or-later
"""#1293 follow-up: a catalog game's folder and prefix are never free text, installed or not.

The support case's player fixed HSR by editing "Game folder" and "Wine prefix" by hand, one at a
time: both fields unlocked because the game showed "Not installed" (the lock only held for an
installed game). Two free fields for one game folder is how install and prefix drift apart.

Now, for a catalog game, both fields are read-only in every state, and one button changes the
location as a whole:
- a copy of the game found elsewhere -> the same "use this folder" chooser as the game page;
- the game's folder holds files (a partial download) -> the normal Move;
- nothing on disk yet -> pick a library; the game will go to ``<library>/Nadare/<Game>``.
Custom Wine games keep their free paths.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QLineEdit, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere

from nadare.core import libraries
from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage

YES = QMessageBox.StandardButton.Yes


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


@pytest.fixture
def rig(tmp_path, qapp, monkeypatch):
    state = {"value": "not_installed"}
    patch_everywhere(
        monkeypatch,
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state=state["value"], data={})
        ),
    )
    shown: list[str] = []
    asked: list[str] = []
    picks: list[Path] = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or YES)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(picks.pop(0)))
    config = Config.load(tmp_path / "config.json")
    page = SettingsPage()
    rig = SimpleNamespace(
        config=config, page=page, shown=shown, asked=asked, picks=picks, state=state, tmp=tmp_path
    )
    yield rig
    page.deleteLater()
    qapp.processEvents()


def _catalog(rig, game: Path, *, make=False):
    if make:
        (game / "install").mkdir(parents=True)
        (game / "install" / "BrownDust II.exe").write_text("x")
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
    rig.config.save()


def _load(rig, qapp, name="bd2"):
    rig.page.load(rig.config, name)
    _settle(qapp)


def _prefix_editor(page) -> QLineEdit:
    editor = page._option_widgets.get("prefix")
    assert isinstance(editor, QLineEdit)
    return editor


def _saved(rig, name="bd2"):
    return Config.load(rig.config.path).profiles[name]


# ---- the fields ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["not_installed", "installed", "error"])
def test_a_catalog_games_folder_and_prefix_are_never_free_text(rig, qapp, state):
    _catalog(rig, rig.tmp / "Games" / "Nadare" / "Brown Dust 2")
    rig.state["value"] = state
    _load(rig, qapp)
    assert rig.page.install_dir.isReadOnly()
    assert not rig.page._install_dir_browse.isEnabled()
    assert _prefix_editor(rig.page).isReadOnly()


def test_a_not_installed_catalog_game_offers_change_location(rig, qapp):
    _catalog(rig, rig.tmp / "Games" / "Nadare" / "Brown Dust 2")
    _load(rig, qapp)
    assert not rig.page.change_location_button.isHidden()
    assert rig.page.change_location_button.isEnabled()


def test_an_installed_game_keeps_move_and_i_moved_it_myself(rig, qapp):
    _catalog(rig, rig.tmp / "Games" / "Nadare" / "Brown Dust 2", make=True)
    rig.state["value"] = "installed"
    _load(rig, qapp)
    assert rig.page.change_location_button.isHidden()
    assert not rig.page.move_button.isHidden()
    assert not rig.page.relink_button.isHidden()


def test_a_custom_wine_game_keeps_free_paths_when_not_installed(rig, qapp, tmp_path):
    rig.config.add_profile(
        "mine", backend="wine", title="Mine", exe_name="game.exe",
        install_dir=str(tmp_path / "mine"), prefix=str(tmp_path / "pfx"),
    )
    rig.config.save()
    _load(rig, qapp, "mine")
    assert not rig.page.install_dir.isReadOnly()
    assert rig.page.change_location_button.isHidden()


def test_saving_never_changes_a_catalog_games_folders_by_text(rig, qapp, tmp_path):
    """Even if something writes the field, Save does not split install and prefix."""
    game = rig.tmp / "Games" / "Nadare" / "Brown Dust 2"
    _catalog(rig, game)
    _load(rig, qapp)
    rig.page.install_dir.setText(str(tmp_path / "elsewhere" / "install"))
    _prefix_editor(rig.page).setText(str(tmp_path / "other" / "prefix"))
    rig.page.save()
    saved = _saved(rig)
    assert saved["install_dir"] == str(game / "install")
    assert saved["prefix"] == str(game / "prefix")


# ---- Change location ----------------------------------------------------------------------


def _hsr_copy(game_dir: Path) -> Path:
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "BrownDust II.exe").write_text("x")
    (game_dir / "prefix" / "drive_c").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-bd2",
                "title": "Brown Dust 2",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    return game_dir.resolve()


def test_change_location_offers_a_copy_found_elsewhere(rig, qapp, monkeypatch):
    lib = rig.tmp / "Games"
    real = _hsr_copy(lib / "Nadare" / "Brown Dust 2")
    rig.config.data[libraries.CONFIG_KEY] = [{"path": str(lib / "Nadare")}]
    _catalog(rig, lib / "Nadare" / "Nadare" / "Brown Dust 2")
    _load(rig, qapp)
    offered: list[list[Path]] = []

    def choose(self, name, copies):
        offered.append([c.game_dir for c in copies])
        return copies[0]

    monkeypatch.setattr(SettingsPage, "_choose_found_copy", choose)
    rig.page.change_location_button.click()
    _settle(qapp)
    assert offered == [[real]]
    saved = _saved(rig)
    assert Path(saved["install_dir"]) == real / "install"
    assert Path(saved["prefix"]) == real / "prefix"


def test_change_location_with_nothing_on_disk_replans_in_a_library(rig, qapp, monkeypatch):
    old = rig.tmp / "gone" / "Nadare" / "Brown Dust 2"
    _catalog(rig, old)
    lib = rig.tmp / "Big"
    lib.mkdir()
    monkeypatch.setattr(
        SettingsPage, "_choose_found_copy", lambda self, n, c: pytest.fail("no copy")
    )
    rig.picks.append(lib)
    _load(rig, qapp)
    rig.page.change_location_button.click()
    _settle(qapp)
    home = lib.resolve() / "Nadare" / "Brown Dust 2"
    saved = _saved(rig)
    assert saved["install_dir"] == str(home / "install")
    assert saved["prefix"] == str(home / "prefix")
    assert saved["owned_dirs"] == [str(home)]
    assert not home.exists()  # nothing is made before the install
    assert lib.resolve() in libraries.library_roots(Config.load(rig.config.path))


def test_change_location_into_a_picked_nadare_folder_means_its_library(rig, qapp):
    _catalog(rig, rig.tmp / "gone" / "Nadare" / "Brown Dust 2")
    picked = rig.tmp / "Big" / "Nadare"
    picked.mkdir(parents=True)
    rig.picks.append(picked)
    _load(rig, qapp)
    rig.page.change_location_button.click()
    _settle(qapp)
    want = (rig.tmp / "Big").resolve() / "Nadare" / "Brown Dust 2" / "install"
    assert _saved(rig)["install_dir"] == str(want)


def test_change_location_with_files_in_the_folder_uses_move(rig, qapp, monkeypatch):
    """A partial download is in the game folder: the location changes only by moving it."""
    game = rig.tmp / "Games" / "Nadare" / "Brown Dust 2"
    _catalog(rig, game, make=True)
    moved: list[str] = []
    monkeypatch.setattr(SettingsPage, "_move_prefix", lambda self: moved.append(self._profile))
    _load(rig, qapp)
    rig.page.change_location_button.click()
    _settle(qapp)
    assert moved == ["bd2"]


def test_cancelling_change_location_changes_nothing(rig, qapp, monkeypatch):
    _catalog(rig, rig.tmp / "gone" / "Nadare" / "Brown Dust 2")
    before = json.dumps(_saved(rig), sort_keys=True)
    monkeypatch.setattr(SettingsPage, "_choose_library", lambda self, c, t, s: None)
    _load(rig, qapp)
    rig.page.change_location_button.click()
    _settle(qapp)
    assert json.dumps(_saved(rig), sort_keys=True) == before
