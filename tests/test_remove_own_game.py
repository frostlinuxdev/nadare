# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare no longer adds or runs your own games (Wine/Proton, Script, any Steam game).

Only catalog games (and the catalog's Steam titles) remain. Existing own-game profiles are
retired once at GUI start: the config is backed up, the profiles are dropped, and the user's
game files and prefixes are never touched.
"""

import json

import pytest
from PyQt6.QtWidgets import QApplication, QPushButton

from nadare.core import owngames, steamsetup
from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui.add_game import AddGameDialog


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_wizard_offers_no_own_game_types():
    kinds = {g.kind for g in add_game.CATALOG}
    assert not kinds & {"wine", "script", "steam"}
    assert not hasattr(add_game, "ADVANCED_GROUP")
    assert {g.group for g in add_game.CATALOG} == {"Games"}


def test_dialog_has_no_advanced_mode(tmp_path, qapp):
    with pytest.raises(TypeError):
        AddGameDialog(Config.load(tmp_path / "config.json"), advanced=True)


def test_store_has_no_add_your_own_game_button(qapp):
    from nadare.gui.store import StorePage  # noqa: PLC0415 - Qt app first

    grid = StorePage()
    try:
        assert not hasattr(grid, "own_game_button")
        assert not hasattr(grid, "own_game_requested")
        assert not any("own game" in b.text().lower() for b in grid.findChildren(QPushButton))
    finally:
        grid.deleteLater()


def _steam_catalog_id() -> str:
    return steamsetup.GAMES[0].app_id


def _write(path, profiles, **top):
    path.write_text(json.dumps({"schema": 1, "profiles": profiles, **top}), encoding="utf-8")


def test_is_own_game():
    assert owngames.is_own_game({"backend": "wine"})
    assert owngames.is_own_game({"backend": "script"})
    assert owngames.is_own_game({"backend": "steam", "steam_app_id": "999999999"})
    assert owngames.is_own_game({"backend": "steam"})
    assert not owngames.is_own_game({"backend": "steam", "steam_app_id": _steam_catalog_id()})
    assert not owngames.is_own_game({"backend": "stove-czn"})


def test_retire_drops_own_games_once_and_backs_up(tmp_path):
    game = tmp_path / "MyGame"
    game.mkdir()
    (game / "game.exe").write_text("x")
    cfg_path = tmp_path / "config.json"
    _write(
        cfg_path,
        {
            "mine": {"backend": "wine", "install_dir": str(game), "prefix": str(game / "pfx")},
            "tool": {"backend": "script"},
            "other-steam": {"backend": "steam", "steam_app_id": "999999999"},
            "endfield": {"backend": "steam", "steam_app_id": _steam_catalog_id()},
            "czn": {"backend": "stove-czn"},
        },
        active_profile="mine",
    )
    before = cfg_path.read_text(encoding="utf-8")
    cfg = Config.load(cfg_path)

    note = owngames.retire(cfg)

    assert set(cfg.profiles) == {"endfield", "czn"}
    assert cfg.data["active_profile"] in {"endfield", "czn"}
    assert "mine" in note and "tool" in note and "other-steam" in note
    assert "not touched" in note
    backups = list(tmp_path.glob("config.json.before-own-games-removed*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == before
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert set(saved["profiles"]) == {"endfield", "czn"}
    assert saved[owngames.RETIRED_FLAG] is True
    assert (game / "game.exe").read_text() == "x"  # files untouched

    again = Config.load(cfg_path)
    assert owngames.retire(again) == ""
    assert len(list(tmp_path.glob("config.json.before-own-games-removed*"))) == 1


def test_retire_without_own_games_writes_nothing(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write(cfg_path, {"czn": {"backend": "stove-czn"}})
    before = cfg_path.read_bytes()
    cfg = Config.load(cfg_path)

    assert owngames.retire(cfg) == ""
    assert cfg_path.read_bytes() == before
    assert not list(tmp_path.glob("config.json.before-own-games-removed*"))


def test_gui_start_retires_own_games(tmp_path, monkeypatch):
    import nadare.gui as gui  # noqa: PLC0415

    cfg_path = tmp_path / "config.json"
    _write(cfg_path, {"mine": {"backend": "wine"}, "czn": {"backend": "stove-czn"}})
    cfg = Config.load(cfg_path)
    notes = gui._startup_notes(cfg)
    assert any("mine" in n for n in notes)
    assert set(cfg.profiles) == {"czn"}


def test_retire_removes_only_the_retired_profiles_menu_shortcuts(tmp_path):
    from nadare.core import shortcuts  # noqa: PLC0415

    apps, desk = tmp_path / "apps", tmp_path / "desk"
    apps.mkdir()
    desk.mkdir()
    for name in ("mine", "czn"):
        shortcuts.install_desktop_entry(
            name, name, applications_dir=apps, desktop_dir=desk, argv=["nadare"]
        )
    foreign = apps / "someone-else.desktop"
    foreign.write_text("[Desktop Entry]\n")
    cfg_path = tmp_path / "config.json"
    _write(cfg_path, {"mine": {"backend": "wine"}, "czn": {"backend": "stove-czn"}})

    owngames.retire(Config.load(cfg_path), applications_dir=apps, desktop_dir=desk)

    left = sorted(p.name for p in [*apps.iterdir(), *desk.iterdir()])
    assert left == ["nadare-czn.desktop", "nadare-czn.desktop", "someone-else.desktop"]
