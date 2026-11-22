# SPDX-License-Identifier: GPL-3.0-or-later
"""#1372 phase 3: an import-move a crash interrupted is settled at the next start.

Before, its journal was read only when the player retried "Move into a library" into the same
library; a kill after a same-disk rename left the game pointing at a folder that was gone
("Not installed") with nothing telling the player how to get it back. Phase 2c offers the move
after every import, so the next start now settles it:

- every tree is in the library: the move finished, only the record was missing; record it;
- otherwise: put the folders that were renamed back where the profile says (a rename, never a
  delete), so the game is exactly as it was before the move;
- anything ambiguous (the profile changed, a folder is in the way, a damaged journal): touch
  nothing, keep the journal, say so.
"""

from __future__ import annotations

import os

from nadare.core import importmove, libraries
from nadare.core.config import Config


def _setup(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    install = tmp_path / "old" / "Game"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "game.pak").write_bytes(b"pak")
    prefix = tmp_path / "old" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile("g", backend="direct-wuwa", title="Game",
                    install_dir=str(install), prefix=str(prefix))
    cfg.save()
    library = tmp_path / "Games"
    library.mkdir()
    return cfg, library, install, prefix


def _killed_after(cfg, library, keys):
    """Journal written and the trees in ``keys`` renamed; then the process died."""
    # As run(): each source gets its marker, then the journal records it (folderproof).
    move = importmove._stamp_sources(importmove.plan(cfg, "g", library))
    importmove._write_journal(move)
    for key, source, destination in move.trees:
        if key in keys:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
    return move


def test_no_journal_changes_nothing(tmp_path):
    cfg, _, install, _ = _setup(tmp_path)
    assert importmove.finish_interrupted(cfg) == []
    assert cfg.profiles["g"]["install_dir"] == str(install)


def test_every_tree_moved_is_recorded_as_finished(tmp_path):
    cfg, library, _, _ = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir", "prefix"})
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "finished" in notes[0]
    profile = again.profiles["g"]
    assert profile["install_dir"] == str(move.home / "install")
    assert profile["prefix"] == str(move.home / "prefix")
    assert profile["owned_dirs"] == [str(move.home)]
    assert (move.home / "install" / "Client" / "game.pak").read_bytes() == b"pak"
    # Kept until the new places are saved (#1486); then it goes.
    assert importmove._journal_path(again).exists()
    again.save()
    assert importmove.forget_if_recorded(again)
    assert not importmove._journal_path(again).exists()


def test_a_half_done_move_is_put_back_by_renaming(tmp_path):
    cfg, library, install, prefix = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir"})
    assert not install.exists()  # the game is "Not installed" right now
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "put back" in notes[0]
    assert (install / "Client" / "game.pak").read_bytes() == b"pak"
    assert (prefix / "drive_c").is_dir()
    assert again.profiles["g"]["install_dir"] == str(install)
    assert "owned_dirs" not in again.profiles["g"]
    assert not move.home.exists()  # the empty game folder Nadare made is gone too
    assert not importmove._journal_path(again).exists()


def test_nothing_moved_yet_only_clears_the_journal(tmp_path):
    cfg, library, install, _ = _setup(tmp_path)
    _killed_after(cfg, library, set())
    again = Config.load(cfg.path)
    importmove.finish_interrupted(again)
    assert install.is_dir()
    assert again.profiles["g"]["install_dir"] == str(install)
    assert not importmove._journal_path(again).exists()


def test_a_folder_in_the_way_is_never_touched(tmp_path):
    cfg, library, install, _ = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir"})
    install.mkdir(parents=True)  # something new where the original was
    (install / "mine.txt").write_text("player's")
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0]
    assert (install / "mine.txt").read_text() == "player's"
    assert (move.home / "install" / "Client" / "game.pak").exists()
    assert importmove._journal_path(again).exists()  # kept for a later look


def test_a_changed_profile_is_never_touched(tmp_path):
    cfg, library, _, _ = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir"})
    again = Config.load(cfg.path)
    again.profiles["g"]["install_dir"] = str(tmp_path / "elsewhere")
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0]
    assert (move.home / "install" / "Client" / "game.pak").exists()
    assert again.profiles["g"]["install_dir"] == str(tmp_path / "elsewhere")
    assert importmove._journal_path(again).exists()


def test_a_journal_for_a_removed_game_is_kept(tmp_path):
    cfg, library, _, _ = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir"})
    again = Config.load(cfg.path)
    again.profiles.pop("g")
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0]
    assert (move.home / "install").exists()
    assert importmove._journal_path(again).exists()


def test_a_damaged_journal_is_kept_and_nothing_moves(tmp_path):
    cfg, _, install, _ = _setup(tmp_path)
    journal = importmove._journal_path(cfg)
    journal.write_text("{not json")
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.read_text() == "{not json"
    assert install.is_dir()


def test_a_tree_outside_the_journals_game_folder_is_refused(tmp_path):
    cfg, library, install, _ = _setup(tmp_path)
    _killed_after(cfg, library, {"install_dir"})
    journal = importmove._journal_path(cfg)
    text = journal.read_text().replace(str(library / "Nadare" / "Game" / "install"),
                                       str(tmp_path / "victim"))
    journal.write_text(text)
    (tmp_path / "victim").mkdir()
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0]
    assert (tmp_path / "victim").is_dir() and not install.exists()


def test_the_next_start_settles_it(tmp_path, monkeypatch):
    """Wired into the start-up upkeep, before anything reads the game's paths."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg, library, install, _ = _setup(tmp_path)
    _killed_after(cfg, library, {"install_dir"})
    again = Config.load(cfg.path)
    assert libraries.backfill(again) is True
    assert (install / "Client" / "game.pak").exists()
    assert not importmove._journal_path(again).exists()


def test_even_an_empty_folder_in_the_way_is_left_alone(tmp_path):
    """A rename would replace an empty folder silently; it is checked before anything moves."""
    cfg, library, install, _ = _setup(tmp_path)
    move = _killed_after(cfg, library, {"install_dir"})
    install.mkdir(parents=True)
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0]
    assert install.is_dir() and not any(install.iterdir())
    assert (move.home / "install" / "Client" / "game.pak").exists()
    assert importmove._journal_path(again).exists()
