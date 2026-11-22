# SPDX-License-Identifier: GPL-3.0-or-later
"""#1299: a Sandbox-on game whose folder the launch would refuse is reported at start-up.

b22 turned the Sandbox on by default and 2026-11-11 made a too-broad game folder stop the launch.
An older profile with no ``secure_mode`` whose folder is the home folder, a folder holding it or
``~/.local/share`` learned that only at Play. Start-up now lists such games once.
"""

from __future__ import annotations

import pytest

from nadare.core import gamesandbox, sandboxreport

KIND = "direct-bd2"  # supports the Sandbox, on by default


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)  # conftest turns it off for old tests
    h = tmp_path / "home"
    (h / ".local" / "share").mkdir(parents=True)
    (h / "Games" / "BD2").mkdir(parents=True)
    return h


def _found(profiles, home):
    found = sandboxreport.broad_folders(profiles, home=home)
    return [(f.profile, f.label, f.reason) for f in found]


def test_a_game_folder_that_is_home_is_reported(home):
    got = _found({"bd2": {"backend": KIND, "install_dir": str(home)}}, home)
    assert got == [("bd2", "game folder", "your home folder")]


def test_a_prefix_in_local_share_is_reported(home):
    got = _found({"bd2": {"backend": KIND, "prefix": str(home / ".local" / "share")}}, home)
    assert got == [("bd2", "prefix", "the folder where your programs keep their data")]


def test_values_under_extra_and_wineprefix_count(home):
    got = _found({"bd2": {"backend": KIND, "extra": {"wineprefix": str(home)}}}, home)
    assert got == [("bd2", "prefix", "your home folder")]


def test_a_fine_folder_is_not_reported(home):
    profile = {"backend": KIND, "install_dir": str(home / "Games" / "BD2")}
    assert _found({"bd2": profile}, home) == []
    assert _found({"bd2": {"backend": KIND}}, home) == [], "no folder named: Nadare's default"


def test_sandbox_off_for_the_game_is_not_reported(home):
    for off in (False, "false"):
        profile = {"backend": KIND, "install_dir": str(home), "secure_mode": off}
        assert _found({"bd2": profile}, home) == []
    profile = {"backend": KIND, "install_dir": str(home), "extra": {"secure_mode": False}}
    assert _found({"bd2": profile}, home) == []


def test_a_kind_without_the_sandbox_or_off_by_default_is_not_reported(home):
    assert _found({"s": {"backend": "steam", "install_dir": str(home)}}, home) == []
    # A kind that cannot sandbox never does, whatever the profile says.
    steam_on = {"backend": "steam", "install_dir": str(home), "secure_mode": True}
    assert _found({"s": steam_on}, home) == []
    assert _found({"w": {"backend": "wine", "install_dir": str(home)}}, home) == []
    # ...unless the player switched it on for that game.
    on = {"backend": "wine", "install_dir": str(home), "secure_mode": True}
    assert _found({"w": on}, home) == [("w", "game folder", "your home folder")]


def test_the_global_default_off_means_nothing_is_reported(home, monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", False)
    assert _found({"bd2": {"backend": KIND, "install_dir": str(home)}}, home) == []


def test_one_line_per_folder_and_unseen_filters_what_was_shown(home):
    profiles = {
        "bd2": {
            "backend": KIND,
            "install_dir": str(home / ".local" / "share"),
            "prefix": str(home),
            "wineprefix": str(home),  # the same folder twice is one line
        },
        "other": {"backend": KIND, "title": "Other Game", "prefix": str(home.parent)},
    }
    found = sandboxreport.broad_folders(profiles, home=home)
    assert [(f.profile, f.label) for f in found] == [
        ("bd2", "prefix"),
        ("bd2", "game folder"),
        ("other", "prefix"),
    ]
    seen = [found[0].key]
    assert sandboxreport.unseen(found, seen) == found[1:]
    assert sandboxreport.unseen(found, None) == found
    text = sandboxreport.message(found[2:])
    assert "Other Game: its prefix is a folder that contains your home folder" in text
    assert "Move to another folder" in text


def test_the_launch_uses_the_same_rule(home):
    """The report must not disagree with the launch: same reason text for the same folder."""
    with pytest.raises(gamesandbox.BroadBindError, match="your home folder"):
        gamesandbox.wrap(["true"], {}, prefix=home / "Games" / "BD2", install_dir=home, home=home)
    assert _found({"bd2": {"backend": KIND, "install_dir": str(home)}}, home)[0][2] == (
        "your home folder"
    )


# ---- GUI wiring -----------------------------------------------------------------------------
class _Cfg:
    def __init__(self, profiles, data=None, notice=None):
        self.profiles = profiles
        self.data = dict(data or {})
        self.notice = notice
        self.saved = 0

    def save(self):
        self.saved += 1


class _Win:
    from nadare.gui.main_window_parts.cleanup import CleanupMixin as _M

    _start_sandbox_broad_check = _M._start_sandbox_broad_check
    _run_sandbox_broad_check = _M._run_sandbox_broad_check
    _show_sandbox_broad = _M._show_sandbox_broad

    def __init__(self, config):
        self.config = config
        self.shown = []

        class _Sig:
            def emit(_s, items):
                self._show_sandbox_broad(items)

        self.sandbox_broad_found = _Sig()


@pytest.fixture
def sync(monkeypatch):
    import threading

    class T:
        def __init__(self, target, args=(), **_k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", T)
    shown = []
    from nadare.gui.main_window_parts import cleanup

    monkeypatch.setattr(
        cleanup.QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append(text)
    )
    return shown


def test_gui_shows_new_ones_once_and_remembers_them(home, sync, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = _Cfg({"bd2": {"backend": KIND, "title": "BD2", "install_dir": str(home)}})
    win = _Win(cfg)
    win._start_sandbox_broad_check()
    assert len(sync) == 1 and "BD2: its game folder is your home folder" in sync[0]
    assert cfg.saved == 1 and len(cfg.data["sandbox_broad_seen"]) == 1
    win._start_sandbox_broad_check()
    assert len(sync) == 1, "told once per game and folder"


def test_gui_nothing_to_say_saves_nothing(home, sync, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = _Cfg({"bd2": {"backend": KIND, "install_dir": str(home / "Games" / "BD2")}})
    _Win(cfg)._start_sandbox_broad_check()
    assert sync == [] and cfg.saved == 0


def test_gui_skips_an_untrusted_config(home, sync, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = _Cfg({"bd2": {"backend": KIND, "install_dir": str(home)}}, notice="recovered")
    _Win(cfg)._start_sandbox_broad_check()
    assert sync == []


def test_gui_a_failing_check_never_escapes(sync, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(sandboxreport, "broad_folders", boom)
    _Win(_Cfg({"g": {"backend": KIND}}))._start_sandbox_broad_check()
    assert sync == []


def test_main_window_runs_the_check_and_config_knows_the_key():
    from pathlib import Path

    from nadare.core import config

    src = Path(__file__).resolve().parents[1] / "src/nadare/gui/main_window.py"
    text = src.read_text()
    assert "_start_sandbox_broad_check" in text and "sandbox_broad_found" in text
    assert config.TOP_DEFAULTS["sandbox_broad_seen"] == []
