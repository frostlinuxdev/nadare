# SPDX-License-Identifier: GPL-3.0-or-later
"""Import-move recovery: the journal outlives an unsaved recovery (#1486), and a finished
tree is adopted only when it is still the very folder the move put there (#1487).

Synthetic folders only; isolated HOME/XDG.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import importmove, relocate
from nadare.core.config import Config

SRC = Path(importmove.__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key, leaf in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    monkeypatch.delenv("NADARE_CHANNEL", raising=False)
    cfg = Config.load(tmp_path / "config.json")
    library = tmp_path / "Games"
    library.mkdir()
    install = tmp_path / "old" / "a" / "install"
    install.mkdir(parents=True)
    (install / "synthetic.pak").write_bytes(b"a")
    prefix = tmp_path / "old" / "a" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    cfg.add_profile("a", backend="direct-wuwa", title="Synthetic a",
                    install_dir=str(install), prefix=str(prefix))
    cfg.save()
    return cfg, library


# --- #1486: the journal stays until the recovered places are saved ---------------------


def test_recovery_survives_process_exit_before_config_save(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    child = """
import os, sys
sys.path.insert(0, sys.argv[1])
from nadare.core import libraries
from nadare.core.config import Config
cfg = Config.load(sys.argv[2])
assert libraries.backfill(cfg)
os._exit(73)  # killed after recovery, before the caller's config.save()
"""
    result = subprocess.run([sys.executable, "-c", child, str(SRC), str(cfg.path)],
                            capture_output=True, text=True, timeout=60, env=os.environ)
    assert result.returncode == 73, result.stderr
    assert move.journal.exists(), "the only record went before the new places were saved"
    again = Config.load(cfg.path)
    importmove.finish_interrupted(again)
    again.save()
    saved = Config.load(cfg.path).profiles["a"]
    assert Path(saved["prefix"], "drive_c").is_dir()
    assert Path(saved["install_dir"], "synthetic.pak").read_bytes() == b"a"


def test_a_failed_save_after_recovery_keeps_the_journal(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    again = Config.load(cfg.path)
    importmove.finish_interrupted(again)
    # The save failed (nothing written): the journal must still be there to retry.
    assert move.journal.exists()
    retry = Config.load(cfg.path)
    notes = importmove.finish_interrupted(retry)
    assert notes and "finished" in notes[0], notes
    assert Path(retry.profiles["a"]["prefix"]).is_dir()


def test_the_journal_goes_once_the_saved_config_names_the_new_places(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    again = Config.load(cfg.path)
    importmove.finish_interrupted(again)
    importmove.forget_if_recorded(again)  # unsaved: kept
    assert move.journal.exists()
    again.save()
    importmove.forget_if_recorded(Config.load(cfg.path))
    assert not move.journal.exists()


def test_the_window_start_saves_then_forgets_the_recovered_journal(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    from nadare.core import libraries

    again = Config.load(cfg.path)
    assert libraries.backfill(again)
    again.save()
    importmove.forget_if_recorded(again)
    assert not move.journal.exists()
    saved = Config.load(cfg.path).profiles["a"]
    assert saved["prefix"].startswith(str(move.home))


# --- #1487: never adopt a replaced destination ------------------------------------------


@pytest.mark.parametrize("copied", [False, True], ids=["rename", "copy"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_fully_moved_recovery_refuses_a_replaced_destination(
    setup, monkeypatch, replacement, copied
):
    cfg, library = setup
    original = dict(cfg.profiles["a"])
    move = importmove.plan(cfg, "a", library)
    if copied:
        monkeypatch.setattr(relocate, "_same_device", lambda *_: False)
    importmove.run(move)
    destination = move.home / "install"
    preserved = move.home.parent / "preserved-original-install"
    destination.rename(preserved)
    unrelated = move.home.parent / "unrelated-folder"
    unrelated.mkdir()
    (unrelated / "personal.txt").write_text("synthetic unrelated content")
    if replacement == "symlink":
        destination.symlink_to(unrelated, target_is_directory=True)
    else:
        destination.mkdir()
        (destination / "personal.txt").write_text("synthetic replacement")
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0], notes
    profile = again.profiles["a"]
    assert profile["install_dir"] == original["install_dir"]
    assert profile["prefix"] == original["prefix"]
    assert not profile.get("owned_paths") and not profile.get("owned_dirs")
    assert move.journal.exists()
    assert (preserved / "synthetic.pak").read_bytes() == b"a"
    assert (destination / "personal.txt").exists()
    assert (unrelated / "personal.txt").exists()


def test_a_replaced_destination_is_not_renamed_back_either(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    # Only the install moved; the prefix went back by hand, then something else took the place.
    prefix_dest = move.home / "prefix"
    source_prefix = next(src for key, src, _ in move.trees if key == "prefix")
    prefix_dest.rename(source_prefix)
    install_dest = move.home / "install"
    aside = move.home.parent / "aside"
    install_dest.rename(aside)
    install_dest.mkdir()
    (install_dest / "other.txt").write_text("not the game")
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0], notes
    assert (install_dest / "other.txt").exists()
    assert move.journal.exists()


def test_a_legacy_journal_without_identities_does_not_adopt_by_existence(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    raw = json.loads(move.journal.read_text())
    for tree in raw["trees"]:
        tree.pop("source_id", None)
        tree.pop("copy_id", None)
    move.journal.write_text(json.dumps(raw))
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "kept" in notes[0], notes
    assert again.profiles["a"]["prefix"] == cfg.profiles["a"]["prefix"]
    assert move.journal.exists()


@pytest.mark.parametrize("copied", [False, True], ids=["rename", "copy"])
def test_an_untouched_finished_move_is_still_adopted(setup, monkeypatch, copied):
    cfg, library = setup
    if copied:
        monkeypatch.setattr(relocate, "_same_device", lambda *_: False)
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "finished" in notes[0], notes
    profile = again.profiles["a"]
    assert profile["install_dir"].startswith(str(move.home))
    assert Path(profile["install_dir"], "synthetic.pak").read_bytes() == b"a"


def test_an_interrupted_rename_is_still_put_back(setup):
    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    # Killed after the first tree: undo the second by hand to fake the state.
    key, source, destination = move.trees[-1]
    destination.rename(source)
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert notes and "put back" in notes[0], notes
    first_key, first_source, _ = move.trees[0]
    assert first_source.is_dir()
    assert not move.journal.exists()


def test_the_window_start_up_step_saves_then_removes_the_journal(setup):
    pytest.importorskip("PyQt6.QtWidgets")
    from types import SimpleNamespace

    from nadare.gui.main_window import MainWindow

    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    again = Config.load(cfg.path)
    MainWindow._backfill_libraries(SimpleNamespace(config=again))
    assert not move.journal.exists()
    assert Config.load(cfg.path).profiles["a"]["prefix"].startswith(str(move.home))


def test_the_window_start_up_step_keeps_the_journal_when_the_save_fails(setup, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from types import SimpleNamespace

    from nadare.gui.main_window import MainWindow

    cfg, library = setup
    move = importmove.plan(cfg, "a", library)
    importmove.run(move)
    again = Config.load(cfg.path)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(again, "save", fail)
    MainWindow._backfill_libraries(SimpleNamespace(config=again))
    assert move.journal.exists()
