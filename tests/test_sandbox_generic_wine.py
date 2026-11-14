# SPDX-License-Identifier: GPL-3.0-or-later
"""The generic Wine game (any .exe the user added) offers the Sandbox, off unless chosen.

Such a game can be anything, anywhere: hiding the home could break it in ways Nadare cannot know,
so it is opt-in there while the publisher launchers and direct games have it on by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends.wine import WineBackend
from nadare.core import backends, gamesandbox
from nadare.core.backends import BackendError


@pytest.fixture
def default_on(monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


def _backend(tmp_path: Path, events: list | None = None, *, exe: Path | None = None, **profile):
    game = tmp_path / "games" / "Thing"
    exe = exe or write_exe(game / "bin" / "thing.exe", "MZ")
    values = {
        "prefix": str(tmp_path / "prefix"),
        "wine_binary": "/usr/bin/wine",
        "install_dir": str(game),
        "exe_name": str(exe),
        **profile,
    }
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True, exist_ok=True)
    ctx = make_context(tmp_path, "wine", values)
    if events is not None:
        ctx.emit = events.append
    return WineBackend(ctx)


def _binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, flag in enumerate(argv) if flag == "--bind"]


def test_generic_wine_offers_the_sandbox():
    assert backends.supports_sandbox("wine") is True
    option = next(o for o in backends.get("wine").options if o.key == "secure_mode")
    assert option.label == "Sandbox" and option.default is False
    assert "home folder" in option.help


def test_it_is_off_unless_chosen_even_with_the_default_on(tmp_path, default_on, has_bwrap):
    argv, _env, _prefix, _target = _backend(tmp_path).build()
    assert Path(argv[0]).name != "bwrap"
    assert gamesandbox.wanted({}, supported=True, default=backends.sandbox_default("wine")) is False


def test_the_sandbox_default_per_kind():
    assert backends.sandbox_default("wine") is False
    assert backends.sandbox_default("direct-pgr") is True
    assert backends.sandbox_default("launcher-nikke") is True
    assert backends.sandbox_default("steam") is False
    assert backends.sandbox_default("no-such-kind") is False


def test_chosen_it_wraps_the_launch_and_binds_the_game_folder(tmp_path, has_bwrap):
    events: list = []
    argv, _env, _prefix, target = _backend(tmp_path, events, secure_mode=True).build()
    assert Path(argv[0]).name == "bwrap"
    assert argv[argv.index("--") + 1 :][-1] == str(target)
    binds = _binds(argv)
    # The whole game folder, not only the exe's bin/ subfolder (data usually sits beside it).
    assert str((tmp_path / "games" / "Thing").resolve()) in binds
    assert str((tmp_path / "prefix").resolve()) in binds
    assert any((e.data or {}).get("sandbox") is True for e in events)


def test_an_exe_outside_the_game_folder_binds_its_own_folder(tmp_path, has_bwrap):
    exe = write_exe(tmp_path / "elsewhere" / "tool.exe", "MZ")
    argv, *_ = _backend(tmp_path, exe=exe, secure_mode=True).build()
    assert str((tmp_path / "elsewhere").resolve()) in _binds(argv)


def test_chosen_without_bubblewrap_stops_the_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(gamesandbox, "locate", lambda **_: gamesandbox.Located(binary=None))
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, secure_mode=True).build()
    assert "bubblewrap" in str(caught.value)


def test_the_window_shows_generic_wine_off_by_default(tmp_path, default_on, monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.profiles["thing"] = {"backend": "wine", "install_dir": str(tmp_path / "g")}
    window = MainWindow(config)
    try:
        window._current_profile = "thing"
        window.game.set_profile("thing", config.profiles["thing"])
        window._sync_sandbox("thing")
        assert not window.game.sandbox.isHidden()
        assert not window.game.sandbox.isChecked()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_a_script_profile_never_sandboxes_its_batch_file(tmp_path, has_bwrap):
    from nadare.backends.script import _BatchWine

    bat = write_exe(tmp_path / "games" / "run.bat", "@echo off")
    values = {
        "prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", "secure_mode": True
    }
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True, exist_ok=True)
    delegate = _BatchWine(make_context(tmp_path, "script", values), bat)
    argv, *_ = delegate.build()
    assert Path(argv[0]).name != "bwrap"
