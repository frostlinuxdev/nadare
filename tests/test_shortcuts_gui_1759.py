# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page adds a game to the app menu and to Steam, and takes it out again (#1759).

Before, only the hidden ``nadare shortcut add`` command could (nadare#4 asked for it).
"""

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import shortcuts
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import shortcuts as gui_shortcuts


@pytest.fixture
def steam(tmp_path, monkeypatch):
    """One Steam account (its config folder exists, no shortcuts.vdf yet); Steam closed."""
    config_dir = tmp_path / "steam" / "userdata" / "1234" / "config"
    config_dir.mkdir(parents=True)
    vdf = config_dir / "shortcuts.vdf"
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    monkeypatch.setattr(shortcuts, "steam_running", lambda *a, **k: False)
    monkeypatch.setattr(shortcuts, "launcher_argv", lambda *a, **k: ["/usr/bin/nadare"])
    return vdf


@pytest.fixture
def apps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: False)
    return tmp_path / "data" / "applications"


@pytest.fixture
def config(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("ark", backend="wine", title="Arknights")
    cfg.add_profile("other", backend="wine")
    return cfg


# ---- the actions (no Qt) ------------------------------------------------------------------


def test_add_to_the_app_menu_and_remove_again(config, apps, steam):
    ok, text = gui_shortcuts.apply_shortcut(config, "ark", "menu-add")
    assert ok and "app menu" in text and "Arknights" in text
    written = list(apps.glob("nadare-*.desktop"))
    assert len(written) == 1
    assert "Name=Arknights" in written[0].read_text()
    assert gui_shortcuts.shortcut_state("ark") == (True, False)
    assert gui_shortcuts.shortcut_state("other") == (False, False)

    ok, text = gui_shortcuts.apply_shortcut(config, "ark", "menu-remove")
    assert ok and "app menu" in text
    assert list(apps.glob("nadare-*.desktop")) == []
    assert gui_shortcuts.shortcut_state("ark") == (False, False)


def test_add_to_steam_and_remove_again(config, apps, steam):
    ok, text = gui_shortcuts.apply_shortcut(config, "ark", "steam-add")
    assert ok and "Steam" in text
    entries = shortcuts.vdf_loads(steam.read_bytes())
    assert [e["AppName"] for e in entries] == ["Arknights"]
    assert gui_shortcuts.shortcut_state("ark") == (False, True)

    ok, _ = gui_shortcuts.apply_shortcut(config, "ark", "steam-remove")
    assert ok
    assert shortcuts.vdf_loads(steam.read_bytes()) == []
    assert gui_shortcuts.shortcut_state("ark") == (False, False)


def test_steam_running_says_to_close_it_and_changes_nothing(config, apps, steam, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda *a, **k: True)
    ok, text = gui_shortcuts.apply_shortcut(config, "ark", "steam-add")
    assert not ok
    assert "Close Steam" in text
    assert not steam.exists()


def test_no_steam_account_says_so(config, apps, monkeypatch):
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [])
    monkeypatch.setattr(shortcuts, "steam_running", lambda *a, **k: False)
    ok, text = gui_shortcuts.apply_shortcut(config, "ark", "steam-add")
    assert not ok
    assert "Steam" in text and "sign in" in text.lower()


def test_a_file_that_is_not_ours_is_reported_not_overwritten(config, apps, steam):
    apps.mkdir(parents=True)
    foreign = apps / "nadare-ark.desktop"
    foreign.write_text("[Desktop Entry]\nName=Someone else\n")
    # The hashed fallback name keeps both; the foreign file stays untouched.
    ok, _ = gui_shortcuts.apply_shortcut(config, "ark", "menu-add")
    assert ok
    assert foreign.read_text() == "[Desktop Entry]\nName=Someone else\n"


def test_an_unknown_profile_or_action_is_refused(config, apps, steam):
    assert gui_shortcuts.apply_shortcut(config, "nope", "menu-add")[0] is False
    with pytest.raises(ValueError):
        gui_shortcuts.apply_shortcut(config, "ark", "menu-explode")


# ---- the game page menu -------------------------------------------------------------------


@pytest.fixture
def view():
    QApplication.instance() or QApplication([])
    v = GameView()
    yield v
    v.deleteLater()


def _texts(view):
    return [a.text() for a in view.more_menu.actions() if not a.isSeparator()]


def test_the_more_menu_offers_add_and_flips_to_remove(view):
    state = {"value": (False, False)}
    view.shortcut_state = lambda profile: state["value"]
    view._profile = "ark"
    view.more_menu.aboutToShow.emit()
    assert "Add to app menu" in _texts(view)
    assert "Add to Steam" in _texts(view)
    state["value"] = (True, True)
    view.more_menu.aboutToShow.emit()
    assert "Remove from app menu" in _texts(view)
    assert "Remove from Steam" in _texts(view)


def test_choosing_an_entry_emits_the_profile_and_action(view):
    view.shortcut_state = lambda profile: (False, True)
    view._profile = "ark"
    view.more_menu.aboutToShow.emit()
    got = []
    view.shortcut_requested.connect(lambda p, a: got.append((p, a)))
    view.menu_shortcut_action.trigger()
    view.steam_shortcut_action.trigger()
    assert got == [("ark", "menu-add"), ("ark", "steam-remove")]


def test_main_window_runs_the_action_and_tells_the_result(tmp_path, monkeypatch, apps, steam):
    app = QApplication.instance() or QApplication([])
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("ark", backend="wine", title="Arknights")
    monkeypatch.setattr(MainWindow, "_probe_all", lambda self: None)
    win = MainWindow(cfg)
    monkeypatch.setattr(win, "_confirm_close", lambda: True)
    told = []
    monkeypatch.setattr(win, "_tell_shortcut", lambda ok, text: told.append((ok, text)))
    win.game.shortcut_requested.emit("ark", "menu-add")
    assert told and told[0][0] is True
    assert win.game.shortcut_state("ark") == (True, False)
    win.close()
    win.deleteLater()
    app.processEvents()
