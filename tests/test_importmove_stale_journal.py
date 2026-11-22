# SPDX-License-Identifier: GPL-3.0-or-later
"""A finished import-move whose record was left behind is recognised and the record dropped.

b24 (and ``next`` before 0946cc89) never removed the import-move journal after a SUCCESSFUL
Settings "Move into a library": only a rolled-back move cleared it. The game already points at
its new folders, so the start-up settle (#1372 phase 3) called the journal "no longer matches
the game", kept it and warned on every start; the support report listed an unfinished
operation that was not there. Seen on a real b24-era config (CZN, prefix-only import).

Now: when every tree the journal names is where the profile already points, and those folders
exist, the move is done; only the record goes. Nothing on disk is touched, the profile is not
rewritten. Anything less than that keeps the old, careful behaviour.
"""

from __future__ import annotations

import json

from nadare.core import importmove, libraries, pending
from nadare.core.config import Config


def _b24_leftover(tmp_path, *, install_inside=False):
    """The state a b24 "Move into a library" leaves: moved, recorded, journal still there.

    The journal is written in b24's exact format (no ``copy_id``), as an old config would
    carry it into the new version.
    """
    library = tmp_path / "Games"
    home = library / "Nadare" / "Game"
    prefix = home / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "save.dat").write_bytes(b"save")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="direct-wuwa", title="Game", prefix=str(prefix))
    profile = cfg.profiles["g"]
    profile["owned_dirs"] = [str(home)]
    profile["owned_trees"] = ["prefix"]
    if install_inside:
        inside = prefix / "drive_c" / "Game"
        inside.mkdir()
        profile["install_dir"] = str(inside)
    cfg.save()
    journal = importmove._journal_path(cfg)
    journal.write_text(json.dumps({
        "home": str(home),
        "install_inside": str(prefix / "drive_c" / "Game") if install_inside else None,
        "library": str(library),
        "profile": "g",
        "trees": [{"destination": str(prefix), "key": "prefix",
                   "source": str(tmp_path / "old" / "prefix")}],
    }))
    return Config.load(cfg.path), journal, prefix


def test_a_finished_moves_leftover_record_is_dropped(tmp_path):
    cfg, journal, prefix = _b24_leftover(tmp_path)
    before = dict(cfg.profiles["g"])
    notes = importmove.finish_interrupted(cfg)
    assert not journal.exists()
    assert notes and "kept" not in notes[0]
    assert cfg.profiles["g"] == before  # nothing rewritten
    assert (prefix / "drive_c" / "save.dat").read_bytes() == b"save"


def test_a_finished_launcher_move_with_the_game_inside_the_prefix_is_dropped(tmp_path):
    cfg, journal, prefix = _b24_leftover(tmp_path, install_inside=True)
    importmove.finish_interrupted(cfg)
    assert not journal.exists()
    assert (prefix / "drive_c" / "Game").is_dir()


def test_the_game_inside_the_prefix_somewhere_else_keeps_the_record(tmp_path):
    cfg, journal, _ = _b24_leftover(tmp_path, install_inside=True)
    cfg.profiles["g"]["install_dir"] = str(tmp_path / "elsewhere")
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists()


def test_a_destination_that_is_gone_keeps_the_record(tmp_path):
    """The profile points at the library but the folder is missing: not provably finished."""
    cfg, journal, prefix = _b24_leftover(tmp_path)
    (prefix / "drive_c" / "save.dat").unlink()
    (prefix / "drive_c").rmdir()
    prefix.rmdir()
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists()


def test_a_profile_pointing_somewhere_else_still_keeps_the_record(tmp_path):
    cfg, journal, _ = _b24_leftover(tmp_path)
    cfg.profiles["g"]["prefix"] = str(tmp_path / "elsewhere")
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists()


def test_a_destination_outside_the_journals_game_folder_keeps_the_record(tmp_path):
    cfg, journal, _ = _b24_leftover(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    data = json.loads(journal.read_text())
    data["trees"][0]["destination"] = str(victim)
    journal.write_text(json.dumps(data))
    cfg.profiles["g"]["prefix"] = str(victim)
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists() and victim.is_dir()


def test_the_original_folder_is_never_touched(tmp_path):
    """A cross-disk b24 move may have kept the original; dropping the record leaves it."""
    cfg, journal, _ = _b24_leftover(tmp_path)
    original = tmp_path / "old" / "prefix"
    (original / "drive_c").mkdir(parents=True)
    (original / "drive_c" / "mine.txt").write_text("player's")
    importmove.finish_interrupted(cfg)
    assert not journal.exists()
    assert (original / "drive_c" / "mine.txt").read_text() == "player's"


def test_the_next_start_drops_it_and_the_report_lists_nothing(tmp_path, monkeypatch):
    """Upgrade path: b24 data opened by this version's start-up upkeep."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg, journal, _ = _b24_leftover(tmp_path)
    assert pending.import_journal_path(cfg.path) == journal
    libraries.backfill(cfg)
    assert not journal.exists()
    again = Config.load(cfg.path)
    importmove.finish_interrupted(again)  # and the start after that is quiet
    assert importmove.finish_interrupted(again) == []


def test_a_destination_that_is_a_link_keeps_the_record(tmp_path):
    """A link where the folder should be is not a finished move Nadare made."""
    cfg, journal, prefix = _b24_leftover(tmp_path)
    real = tmp_path / "real-prefix"
    prefix.rename(real)
    prefix.symlink_to(real, target_is_directory=True)
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists() and (real / "drive_c" / "save.dat").exists()


def test_a_journal_naming_an_unknown_setting_keeps_the_record(tmp_path):
    cfg, journal, prefix = _b24_leftover(tmp_path)
    data = json.loads(journal.read_text())
    data["trees"][0]["key"] = "covers"
    journal.write_text(json.dumps(data))
    cfg.profiles["g"]["covers"] = str(prefix)
    notes = importmove.finish_interrupted(cfg)
    assert notes and "kept" in notes[0]
    assert journal.exists()
