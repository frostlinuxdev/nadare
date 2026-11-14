# SPDX-License-Identifier: GPL-3.0-or-later
"""#1293 items 2 and 3: Find games and Move no longer lead back into ``Nadare/Nadare``.

Item 2: Libraries -> Find games used to skip a game that already had a profile ("already in your
Library"), even when that profile pointed at a folder that no longer existed. It now offers
"use this folder" for the existing game.

Item 3: Move / Move into a library opened a free folder picker; picking a folder named ``Nadare``
(the mistake behind the support case) made ``Nadare/Nadare/<Game>`` again. The picker now lists
the libraries first, and any picked ``Nadare`` folder means its parent library.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere

from nadare.core import libraries
from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.libraries_panel import LibrariesPanel
from nadare.gui.settings import SettingsPage

YES = QMessageBox.StandardButton.Yes


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp) -> None:
    for _ in range(3):
        assert QThreadPool.globalInstance().waitForDone(10_000)
        qapp.processEvents()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    drive = tmp_path / "drive" / "Gacha games"
    drive.mkdir(parents=True)
    return Config.load(tmp_path / "config.json"), drive


def _hsr(game_dir: Path) -> Path:
    (game_dir / "install").mkdir(parents=True)
    (game_dir / "install" / "StarRail.exe").write_text("exe")
    (game_dir / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (game_dir / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-hsr",
                "title": "Honkai: Star Rail",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    return game_dir


def _broken_profile(cfg: Config, old: Path) -> None:
    cfg.add_profile(
        "hsr",
        backend="direct-hsr",
        title="Honkai: Star Rail",
        install_dir=str(old / "install"),
        prefix=str(old / "prefix"),
        owned_dirs=[str(old)],
        owned_trees=["install_dir", "prefix"],
    )


# ---- item 2: Find games ---------------------------------------------------------------------


def _panel(cfg, qapp, answer=True):
    asked: list[list[tuple[str, bool]]] = []

    def choose(items):
        asked.append(list(items))
        return [checked for _label, checked in items] if answer else None

    panel = LibrariesPanel(choose=choose)
    panel.load(cfg)
    qapp.processEvents()
    return panel, asked


def test_find_games_offers_the_folder_for_a_game_already_in_the_library(env, qapp):
    cfg, drive = env
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    real = _hsr(drive / "Nadare" / "Honkai Star Rail").resolve()
    _broken_profile(cfg, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    cfg.save()
    panel, asked = _panel(cfg, qapp)
    repaired: list[list[str]] = []
    panel.games_repaired.connect(repaired.append)
    panel.find_button.click()
    _settle(qapp)
    assert len(asked) == 1
    ((label, ticked),) = asked[0]
    assert "Honkai: Star Rail" in label and "already in your Library" in label
    assert ticked  # a found copy of a broken game starts ticked
    prof = cfg.profiles["hsr"]
    assert Path(prof["install_dir"]) == real / "install"
    assert list(cfg.profiles) == ["hsr"]  # re-pointed, not a second profile
    assert Path(Config.load(cfg.path).profiles["hsr"]["install_dir"]) == real / "install"
    assert repaired == [["hsr"]]
    panel.deleteLater()


def test_find_games_leaves_a_working_game_alone(env, qapp):
    cfg, drive = env
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    used = _hsr(drive / "Nadare" / "Honkai Star Rail")
    _hsr(drive / "Nadare" / "Nadare" / "Honkai Star Rail")  # a second copy
    _broken_profile(cfg, used)  # the profile's folders exist: nothing is broken
    cfg.save()
    before = json.dumps(cfg.data, sort_keys=True)
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    _settle(qapp)
    assert asked == []
    assert json.dumps(cfg.data, sort_keys=True) == before
    panel.deleteLater()


def test_find_games_unticked_repair_changes_nothing(env, qapp):
    cfg, drive = env
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    _hsr(drive / "Nadare" / "Honkai Star Rail")
    _broken_profile(cfg, drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    cfg.save()
    before = json.dumps(cfg.data, sort_keys=True)
    panel = LibrariesPanel(choose=lambda items: [False] * len(items))
    panel.load(cfg)
    panel.find_button.click()
    _settle(qapp)
    assert json.dumps(cfg.data, sort_keys=True) == before
    panel.deleteLater()


# ---- item 3: the Move pickers ----------------------------------------------------------------


def test_the_library_picker_lists_the_libraries_and_another_folder(env, qapp):
    from nadare.gui.library_picker import ANOTHER_FOLDER, LibraryPicker

    cfg, drive = env
    other = drive.parent / "Other"
    other.mkdir()
    gone = drive.parent / "Unplugged"
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}, {"path": str(other)}]
    cfg.data[libraries.CONFIG_KEY].append({"path": str(gone)})  # stored, folder gone
    picker = LibraryPicker(cfg, "Move Honkai: Star Rail into which library?")
    rows = [picker.list.item(i) for i in range(picker.list.count())]
    texts = [r.text() for r in rows]
    assert texts[-1] == ANOTHER_FOLDER
    assert any(str(drive.resolve()) in t for t in texts)
    assert any(str(other.resolve()) in t for t in texts)
    assert any("free" in t for t in texts)
    # A library whose folder is gone is shown but cannot be picked.
    gone_rows = [r for r in rows if str(gone) in r.text()]
    assert gone_rows and not gone_rows[0].flags() & gone_rows[0].flags().ItemIsEnabled
    # Picking a library returns its folder.
    for i, row in enumerate(rows):
        if str(other.resolve()) in row.text():
            picker.list.setCurrentRow(i)
    assert picker.chosen() == other.resolve()
    picker.deleteLater()


def test_another_folder_named_nadare_means_its_library(env, qapp, monkeypatch):
    from nadare.gui.library_picker import resolve_other_folder

    _cfg, drive = env
    (drive / "Nadare").mkdir()
    assert resolve_other_folder(str(drive / "Nadare")) == drive.resolve()
    assert resolve_other_folder(str(drive)) == drive.resolve()


def _settings_rig(tmp_path, qapp, monkeypatch, cfg, picks):
    patch_everywhere(
        monkeypatch,
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="installed", data={})
        ),
    )
    rig = SimpleNamespace(questions=[], shown=[], picks=list(picks))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: rig.questions.append(a[2]) or YES)
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: rig.shown.append(a[2]))
    # The library picker hands back whatever the user picked (as conftest routes it).
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(rig.picks.pop(0))
    )
    rig.page = SettingsPage()
    return rig


def test_move_into_a_picked_nadare_folder_does_not_make_nadare_nadare(env, qapp, monkeypatch,
                                                                       tmp_path):
    cfg, drive = env
    home = _hsr(drive / "Nadare" / "Honkai Star Rail")
    cfg.add_profile(
        "hsr",
        backend="direct-hsr",
        title="Honkai: Star Rail",
        install_dir=str(home / "install"),
        prefix=str(home / "prefix"),
        owned_dirs=[str(home)],
        owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str(home / "install"), "prefix": str(home / "prefix")},
    )
    cfg.save()
    target = tmp_path / "big" / "Nadare"
    target.mkdir(parents=True)
    rig = _settings_rig(tmp_path, qapp, monkeypatch, cfg, [target])
    rig.page.load(cfg, "hsr")
    _settle(qapp)
    rig.page.move_button.click()
    _settle(qapp)
    saved = Config.load(cfg.path).profiles["hsr"]
    want = (tmp_path / "big" / "Nadare" / "Honkai Star Rail").resolve()
    assert Path(saved["install_dir"]).resolve() == want / "install", rig.shown
    assert not (tmp_path / "big" / "Nadare" / "Nadare").exists()
    rig.page.deleteLater()
    qapp.processEvents()


def test_the_move_pickers_ask_through_the_library_picker(env, qapp, monkeypatch, tmp_path):
    """Both Move flows go through ``_choose_library`` (the list of libraries), never a bare
    folder dialog of their own."""
    cfg, drive = env
    home = _hsr(drive / "Nadare" / "Honkai Star Rail")
    cfg.add_profile(
        "hsr",
        backend="direct-hsr",
        title="Honkai: Star Rail",
        install_dir=str(home / "install"),
        prefix=str(home / "prefix"),
        owned_dirs=[str(home)],
        owned_trees=["install_dir", "prefix"],
        owned_paths={"install_dir": str(home / "install"), "prefix": str(home / "prefix")},
    )
    cfg.save()
    rig = _settings_rig(tmp_path, qapp, monkeypatch, cfg, [])
    asked: list[str] = []
    monkeypatch.setattr(
        SettingsPage, "_choose_library", lambda self, config, title, start: asked.append(title)
    )
    rig.page.load(cfg, "hsr")
    _settle(qapp)
    rig.page.move_button.click()
    _settle(qapp)
    assert len(asked) == 1 and "Honkai: Star Rail" in asked[0]
    assert Path(Config.load(cfg.path).profiles["hsr"]["install_dir"]) == home / "install"
    rig.page.deleteLater()
    qapp.processEvents()


def test_the_default_library_is_pickable_before_its_folder_exists(env, qapp):
    """Nadare makes its own library folder on demand; it is never "unplugged"."""
    from PyQt6.QtCore import Qt

    from nadare.gui.library_picker import LibraryPicker

    cfg, _drive = env
    assert not libraries.default_root().exists()
    picker = LibraryPicker(cfg, "Move into which library?")
    first = picker.list.item(0)
    assert "Default library" in first.text()
    assert first.flags() & Qt.ItemFlag.ItemIsEnabled
    assert picker.chosen() == libraries.default_root()
    picker.deleteLater()


def test_move_into_a_library_maps_a_picked_nadare_folder_to_its_library(
    env, qapp, monkeypatch, tmp_path
):
    """Move into a library: a picked ``X/Nadare`` puts an import at ``X/Nadare/<Game>``."""
    cfg, _drive = env
    install = tmp_path / "old" / "WW"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "g.pak").write_bytes(b"x" * 100)
    prefix = tmp_path / "old" / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(install), prefix=str(prefix),
    )
    cfg.save()
    picked = tmp_path / "Games" / "Nadare"
    picked.mkdir(parents=True)
    rig = _settings_rig(tmp_path, qapp, monkeypatch, cfg, [picked])
    rig.page.load(cfg, "wuwa")
    _settle(qapp)
    rig.page.import_move_button.click()
    _settle(qapp)
    saved = Config.load(cfg.path).profiles["wuwa"]
    want = (tmp_path / "Games" / "Nadare" / "Wuthering Waves").resolve()
    assert Path(saved["install_dir"]).resolve() == want / "install", rig.shown
    assert not (tmp_path / "Games" / "Nadare" / "Nadare").exists()
    rig.page.deleteLater()
    qapp.processEvents()


def test_find_games_in_the_panel_offers_a_b21_layout_game(env, qapp):
    """The core scan reads ``X/Nadare/Nadare/<Game>`` (#1296), but the panel's own "is it in a
    library" filter used ``parent.parent`` and threw such games away (disjoint audit 2026-11-13)."""
    cfg, drive = env
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    game = _hsr(drive / "Nadare" / "Nadare" / "Honkai Star Rail")
    cfg.save()
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    _settle(qapp)
    assert len(asked) == 1, panel.message.text()
    ((label, _ticked),) = asked[0]
    assert "Honkai: Star Rail" in label
    assert "hsr" in " ".join(cfg.profiles) or any(
        p.get("backend") == "direct-hsr" for p in cfg.profiles.values()
    )
    assert Path(next(iter(cfg.profiles.values()))["install_dir"]).resolve() == (
        game.resolve() / "install"
    )
    panel.deleteLater()


def test_find_games_in_the_panel_offers_a_default_library_game(env, qapp):
    """A game left in Nadare's own ``profiles/<name>`` (config lost) is offered back too."""
    cfg, _drive = env
    _hsr(libraries.default_root() / "hsr-old")
    panel, asked = _panel(cfg, qapp)
    panel.find_button.click()
    _settle(qapp)
    assert len(asked) == 1, panel.message.text()
    panel.deleteLater()


def test_the_move_picker_does_not_offer_nadares_own_folder(env, qapp):
    """Nadare's default library keeps games as ``profiles/<name>``; a moved game would land in
    ``profiles/Nadare/<Game>``, where Find games never looks (disjoint audit W1-1)."""
    from nadare.gui.library_picker import ANOTHER_FOLDER, LibraryPicker

    cfg, drive = env
    cfg.data[libraries.CONFIG_KEY] = [{"path": str(drive)}]
    picker = LibraryPicker(cfg, "Move into which library?", include_default=False)
    texts = [picker.list.item(i).text() for i in range(picker.list.count())]
    assert not any("Default library" in t for t in texts)
    assert texts[-1] == ANOTHER_FOLDER
    picker.deleteLater()


def test_moving_into_nadares_own_folder_is_refused_clearly(tmp_path):
    from nadare.core import relink

    game = tmp_path / "Games" / "Nadare" / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / "prefix" / "drive_c").mkdir(parents=True)
    profile = {
        "backend": "direct-bd2", "prefix": str(game / "prefix"),
        "install_dir": str(game / "install"), "owned_dirs": [str(game)],
        "owned_trees": ["install_dir", "prefix"],
    }
    with pytest.raises(relink.RelinkError, match="(?i)nadare's own"):
        relink.check_home_target(profile, libraries.default_root())
