# SPDX-License-Identifier: GPL-3.0-or-later
"""A catalog game whose program is named by a path (NTE) is found by import and by libraries.

NTE's exe is ``Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe``: comparing file names two
levels deep never matched it, so importing an existing NTE folder silently found nothing
(measured on the rig, 2026-11-10).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.core import libraries
from nadare.gui.add_game import _find_exe, detect_import

NTE_EXE = "Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe"


def _nte(folder):
    exe = folder / NTE_EXE
    exe.parent.mkdir(parents=True)
    exe.write_text("MZ")
    (folder / "NTEGlobal").mkdir()
    return exe


def test_find_exe_finds_a_program_named_by_a_path(tmp_path):
    exe = _nte(tmp_path / "game")
    assert _find_exe(tmp_path / "game", (NTE_EXE,)) == exe


def test_find_exe_still_finds_plain_names(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "Game.exe").write_text("MZ")
    assert _find_exe(tmp_path, ("Game.exe",)) == tmp_path / "a" / "b" / "Game.exe"


def test_a_path_never_escapes_the_folder(tmp_path):
    (tmp_path / "outside.exe").write_text("MZ")
    (tmp_path / "game").mkdir()
    assert _find_exe(tmp_path / "game", ("../outside.exe",)) is None


def test_importing_an_existing_nte_folder_finds_it(tmp_path):
    _nte(tmp_path / "game")
    info = detect_import("direct-nte", str(tmp_path / "game"), str(tmp_path / "prefix"))
    assert info["install_dir"] == str(tmp_path / "game")


def test_a_library_recognises_nte_by_its_program_path(tmp_path):
    game_dir = tmp_path / "Lib" / libraries.NADARE_DIR / "Neverness to Everness"
    _nte(game_dir / "install")
    spec = libraries.GameSpec(kind="direct-nte", title="Neverness to Everness", exes=(NTE_EXE,))
    assert libraries.match_game(game_dir, spec) == (game_dir / "install", None)
