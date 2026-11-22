# SPDX-License-Identifier: GPL-3.0-or-later
"""#1400: an import-move to another disk, killed after its first folder was copied.

The original stays (a copy, not a rename), so the next start could not "put it back" and kept a
full copy of the game in the library plus its journal, for good. Now the run records the copy's
directory identity in the journal; the next start removes only a copy it can prove it made
(same policy as an interrupted Move), and keeps anything it cannot prove.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from nadare.core import importmove, relocate
from nadare.core.config import Config


@pytest.fixture
def cross_disk(monkeypatch):
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)


def _setup(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    install = tmp_path / "old" / "Game"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "game.pak").write_bytes(b"pak")
    prefix = tmp_path / "old" / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "system.reg").write_text("reg")
    cfg.add_profile("g", backend="direct-wuwa", title="Game",
                    install_dir=str(install), prefix=str(prefix))
    cfg.save()
    library = tmp_path / "Games"
    library.mkdir()
    return cfg, library, install, prefix


def _killed_after_first_copy(cfg, library, monkeypatch):
    """Run the real move; the process dies when the second folder starts."""
    move = importmove.plan(cfg, "g", library)
    real = relocate.move_tree
    calls = []
    seen = {}

    class Killed(BaseException):
        pass

    def dying(src, dst, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            seen["journal"] = json.loads(move.journal.read_text())
            raise Killed
        return real(src, dst, **kwargs)

    monkeypatch.setattr(relocate, "move_tree", dying)
    monkeypatch.setattr(importmove, "_undo", lambda *a: None)  # killed: no rollback runs
    monkeypatch.setattr(importmove, "_clear_journal", lambda m: None)
    with pytest.raises(Killed):
        importmove.run(move)
    monkeypatch.undo()
    return move, seen["journal"]


def test_run_records_the_copy_identity(tmp_path, monkeypatch, cross_disk):
    cfg, library, install, _ = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, journal = _killed_after_first_copy(cfg, library, monkeypatch)
    first = journal["trees"][0]
    assert first["key"] == "install_dir"
    dst = move.trees[0][2]
    st = os.lstat(dst)
    assert first["copy_id"] == [st.st_dev, st.st_ino]
    assert "copy_id" not in journal["trees"][1]
    assert install.is_dir()  # a copy: the original is still there


def test_next_start_removes_its_verified_copy(tmp_path, monkeypatch, cross_disk):
    cfg, library, install, prefix = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, _ = _killed_after_first_copy(cfg, library, monkeypatch)
    copy = move.trees[0][2]
    assert copy.is_dir()
    again = Config.load(cfg.path)
    notes = importmove.finish_interrupted(again)
    assert not copy.exists() and not move.home.exists()
    assert (install / "Client" / "game.pak").read_bytes() == b"pak"  # original untouched
    assert (prefix / "system.reg").read_text() == "reg"
    assert not move.journal.exists()
    assert again.profiles["g"]["install_dir"] == str(install)
    assert notes and "removed" in notes[0]


def test_a_copy_without_a_recorded_identity_is_kept(tmp_path, monkeypatch, cross_disk):
    """A journal from before this fix (or a kill before the rewrite): nothing is deleted."""
    cfg, library, install, _ = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, _ = _killed_after_first_copy(cfg, library, monkeypatch)
    data = json.loads(move.journal.read_text())
    for tree in data["trees"]:
        tree.pop("copy_id", None)
    move.journal.write_text(json.dumps(data))
    copy = move.trees[0][2]
    notes = importmove.finish_interrupted(Config.load(cfg.path))
    assert copy.is_dir() and move.journal.exists() and install.is_dir()
    assert notes and "kept" in notes[0]


def test_a_replaced_copy_is_kept(tmp_path, monkeypatch, cross_disk):
    """The folder at the destination is not the one the run made: never deleted."""
    cfg, library, install, _ = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, _ = _killed_after_first_copy(cfg, library, monkeypatch)
    copy = move.trees[0][2]
    shutil.rmtree(copy)
    copy.mkdir()
    (copy / "player.txt").write_text("mine")
    notes = importmove.finish_interrupted(Config.load(cfg.path))
    assert (copy / "player.txt").read_text() == "mine"
    assert move.journal.exists() and install.is_dir()
    assert notes and "kept" in notes[0]


def test_a_retry_from_settings_still_finishes(tmp_path, monkeypatch, cross_disk):
    cfg, library, install, prefix = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, _ = _killed_after_first_copy(cfg, library, monkeypatch)
    again = Config.load(cfg.path)
    retry = importmove.plan(again, "g", library)
    assert retry.done == ("install_dir",)
    importmove.run(retry)
    importmove.commit(again, "g", retry)
    assert again.profiles["g"]["prefix"] == str(retry.home / "prefix")
    assert (retry.home / "install" / "Client" / "game.pak").read_bytes() == b"pak"
    assert not retry.journal.exists()


def test_a_copy_that_cannot_be_fully_removed_keeps_the_journal(tmp_path, monkeypatch, cross_disk):
    cfg, library, install, _ = _setup(tmp_path)
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    move, _ = _killed_after_first_copy(cfg, library, monkeypatch)
    from nadare.core import removal

    real = removal._rmtree_safely

    def partly(path):
        outcome = real(path)
        outcome.leftovers.append((path / "stuck", OSError("busy")))
        return outcome

    monkeypatch.setattr(removal, "_rmtree_safely", partly)
    notes = importmove.finish_interrupted(Config.load(cfg.path))
    assert move.journal.exists() and install.is_dir()
    assert notes and "could not remove" in notes[0]
