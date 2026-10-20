# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI tests for ``nadare shortcut`` using the hermetic XDG tree."""

from __future__ import annotations

import os
from pathlib import Path

from nadare.app import run_cli
from nadare.core.config import Config


def _make_config() -> None:
    cfg = Config.load()
    cfg.add_profile("game", title="My Game")
    cfg.save()


def test_shortcut_add_installs_menu_entry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nadare.core.shortcuts.shutil.which", lambda name: None)
    _make_config()

    assert run_cli(["shortcut", "add", "-p", "game"]) == 0

    entry = Path(os.environ["XDG_DATA_HOME"]) / "applications" / "nadare-game.desktop"
    assert entry.exists()
    text = entry.read_text(encoding="utf-8")
    assert "X-Nadare-Profile=game" in text
    assert "launch -p game" in text


def test_shortcut_add_desktop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nadare.core.shortcuts.shutil.which", lambda name: None)
    desktop = tmp_path / "Desktop"
    monkeypatch.setattr("nadare.app._desktop_dir", lambda: desktop)
    _make_config()

    assert run_cli(["shortcut", "add", "-p", "game", "--desktop"]) == 0
    assert (desktop / "nadare-game.desktop").exists()

    assert run_cli(["shortcut", "remove", "-p", "game"]) == 0
    assert not (desktop / "nadare-game.desktop").exists()


def test_shortcut_unknown_profile(monkeypatch, tmp_path, capsys) -> None:
    _make_config()
    assert run_cli(["shortcut", "add", "-p", "nope"]) == 1
    assert "no such profile" in capsys.readouterr().err
