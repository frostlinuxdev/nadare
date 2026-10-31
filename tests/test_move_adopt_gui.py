# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F401, F811
"""Settings Move: the button says what moves; a profiles-folder game is converted."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from nadare.core import paths
from tests.test_move_layout_gui import _make, _settle, make, qapp


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    root = tmp_path / "data" / "profiles"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    return root


def _legacy(root, *, install: bool) -> dict:
    home = root / "nikke-goddess-of-victory"
    prefix = home / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    if install:
        (home / "install").mkdir()
        (home / "install" / "game.exe").write_bytes(b"MZ" * 50)
    return {
        "backend": "wine",
        "title": "NIKKE",
        "exe_name": "game.exe",
        "prefix": str(prefix),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }


def test_a_profiles_folder_game_without_a_game_folder_moves_into_the_library(
    make, tmp_path, profiles_dir
):
    lib = tmp_path / "izuna"
    lib.mkdir()
    rig = make([lib], {"nikke": _legacy(profiles_dir, install=False)})
    data = rig.move("nikke")
    home = lib / "Nadare" / "NIKKE"
    assert data["prefix"] == str(home / "prefix")
    assert data["install_dir"] == str(home / "install")
    assert (home / "prefix" / "drive_c").is_dir()
    assert not (profiles_dir / "nikke-goddess-of-victory").exists()
    assert "game folder and its Wine prefix" in rig.questions[0]
    assert "does not exist yet" in rig.questions[0]


def test_the_button_names_what_it_moves_for_a_managed_game(make, tmp_path):
    rig = make([], {"g": _make(tmp_path / "lib")})
    rig.page.load(rig.config, "g")
    _settle(rig.qapp)
    assert rig.page.move_button.text() == "Move game and prefix…"
    assert "game folder and its Wine prefix" in rig.page.move_button.toolTip()


def test_the_button_says_prefix_only_for_a_hand_picked_layout(make, tmp_path):
    rig = make([], {"g": _make(tmp_path / "lib", managed=False)})
    rig.page.load(rig.config, "g")
    _settle(rig.qapp)
    assert rig.page.move_button.text() == "Move prefix only…"
    assert "only the Wine prefix" in rig.page.move_button.toolTip()


def test_picking_the_folder_the_prefix_is_already_in_is_refused(make, tmp_path):
    profile = _make(tmp_path / "lib", managed=False)
    prefix = profile["prefix"]
    rig = make([tmp_path / "lib" / "Game"], {"g": profile})
    rig.move("g")
    assert rig.questions == []
    assert any("already" in text.lower() for text in rig.shown)
    assert (tmp_path / "lib" / "Game" / "prefix").is_dir() and prefix == profile["prefix"]
