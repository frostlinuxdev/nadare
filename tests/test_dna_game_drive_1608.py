# SPDX-License-Identifier: GPL-3.0-or-later
"""Duet Night Abyss gets a drive letter for its game folder (#1608).

On a first run the publisher's sign-in program (PcUsdkexe) builds its save folder and walks the
parent folders of the game's path. From ``Z:\\home\\...`` (any game folder outside ``drive_c``,
which is what Add Game makes) Wine fails one of those checks, the program crashes on the error
text and the game waits forever: a black screen with a cursor. With the game folder as its own
drive (``D:\\EM\\...``) the same first run reaches the game (an AMD PC, 2026-11-18, Sandbox on).
"""

from __future__ import annotations

import os
from pathlib import Path

from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import gamedrive


def _prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    devices = prefix / "dosdevices"
    devices.mkdir()
    os.symlink("../drive_c", devices / "c:")
    os.symlink("/", devices / "z:")
    return prefix


def _game(tmp_path: Path, name: str = "install") -> Path:
    game = tmp_path / name
    game.mkdir()
    (game / "EM.exe").write_bytes(b"MZ")
    return game


def _link(prefix: Path, letter: str) -> str:
    return os.readlink(prefix / "dosdevices" / f"{letter}:")


def test_game_outside_drive_c_gets_the_preferred_letter(tmp_path):
    prefix, game = _prefix(tmp_path), _game(tmp_path)
    assert gamedrive.ensure(prefix, game, "d", exe="EM.exe") == "d"
    assert _link(prefix, "d") == str(game)


def test_game_inside_drive_c_needs_no_letter(tmp_path):
    prefix = _prefix(tmp_path)
    game = prefix / "drive_c" / "Games" / "DuetNightAbyss"
    game.mkdir(parents=True)
    assert gamedrive.ensure(prefix, game, "d", exe="EM.exe") is None
    assert not os.path.lexists(prefix / "dosdevices" / "d:")


def test_an_existing_link_to_the_game_is_kept(tmp_path):
    prefix, game = _prefix(tmp_path), _game(tmp_path)
    os.symlink(game, prefix / "dosdevices" / "f:")
    assert gamedrive.ensure(prefix, game, "d", exe="EM.exe") == "f"
    assert not os.path.lexists(prefix / "dosdevices" / "d:")


def test_someone_elses_drive_is_never_touched(tmp_path):
    prefix, game = _prefix(tmp_path), _game(tmp_path)
    other = tmp_path / "data"
    other.mkdir()
    os.symlink(other, prefix / "dosdevices" / "d:")
    letter = gamedrive.ensure(prefix, game, "d", exe="EM.exe")
    assert letter not in (None, "d", "c", "s", "z")
    assert _link(prefix, "d") == str(other)
    assert _link(prefix, letter) == str(game)


def test_a_dead_link_is_taken_over(tmp_path):
    prefix, game = _prefix(tmp_path), _game(tmp_path)
    os.symlink(tmp_path / "gone", prefix / "dosdevices" / "d:")
    assert gamedrive.ensure(prefix, game, "d", exe="EM.exe") == "d"
    assert _link(prefix, "d") == str(game)


def test_a_link_to_an_old_copy_of_the_game_is_moved(tmp_path):
    prefix = _prefix(tmp_path)
    old, new = _game(tmp_path, "old"), _game(tmp_path, "new")
    os.symlink(old, prefix / "dosdevices" / "d:")
    assert gamedrive.ensure(prefix, new, "d", exe="EM.exe") == "d"
    assert _link(prefix, "d") == str(new)


def test_no_prefix_yet_means_no_letter(tmp_path):
    game = _game(tmp_path)
    assert gamedrive.ensure(tmp_path / "missing", game, "d", exe="EM.exe") is None


def test_all_letters_taken_means_no_letter(tmp_path):
    prefix, game = _prefix(tmp_path), _game(tmp_path)
    other = tmp_path / "data"
    other.mkdir()
    for letter in "abdefghijklmnopqrstuvwxy":
        os.symlink(other, prefix / "dosdevices" / f"{letter}:")
    assert gamedrive.ensure(prefix, game, "d", exe="EM.exe") is None


def test_only_dna_asks_for_a_game_drive():
    assert direct.DnaBackend.game_drive_letter == "d"
    others = [
        cls
        for cls in vars(direct).values()
        if isinstance(cls, type)
        and issubclass(cls, direct.DirectBackend)
        and cls is not direct.DnaBackend
    ]
    assert others
    assert all(cls.game_drive_letter is None for cls in others)


def _launch(tmp_path, monkeypatch, cls, kind, game):
    prefix = _prefix(tmp_path)
    ctx = make_context(tmp_path, kind, {"prefix": str(prefix), "install_dir": str(game)})
    backend = cls(ctx)
    monkeypatch.setattr(backend, "direct_target", lambda: game / "EM.exe")
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_launch_direct", lambda: 0)
    assert backend.launch() == 0
    return prefix


def test_dna_launch_links_its_game_folder(tmp_path, monkeypatch):
    game = _game(tmp_path)
    prefix = _launch(tmp_path, monkeypatch, direct.DnaBackend, "direct-dna", game)
    assert _link(prefix, "d") == str(game)


def test_other_games_launch_without_a_game_drive(tmp_path, monkeypatch):
    game = _game(tmp_path)
    prefix = _launch(tmp_path, monkeypatch, direct.SnowbreakBackend, "direct-snowbreak", game)
    assert sorted(p.name for p in (prefix / "dosdevices").iterdir()) == ["c:", "z:"]
