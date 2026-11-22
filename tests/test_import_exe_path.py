# SPDX-License-Identifier: GPL-3.0-or-later
"""Libraries recognise NTE by its nested program path."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.core import libraries

NTE_EXE = "Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe"


def _nte(folder):
    exe = folder / NTE_EXE
    exe.parent.mkdir(parents=True)
    exe.write_text("MZ")
    (folder / "NTEGlobal").mkdir()
    return exe


def test_a_library_recognises_nte_by_its_program_path(tmp_path):
    game_dir = tmp_path / "Lib" / libraries.NADARE_DIR / "Neverness to Everness"
    _nte(game_dir / "install")
    spec = libraries.GameSpec(kind="direct-nte", title="Neverness to Everness", exes=(NTE_EXE,))
    assert libraries.match_game(game_dir, spec) == (game_dir / "install", None)
