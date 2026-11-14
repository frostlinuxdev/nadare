# SPDX-License-Identifier: GPL-3.0-or-later
"""The import-move journal lives until commit() recorded the new places (#1307 I6).

A process killed after the last tree moved but before the profile was saved must
still be finishable: the next plan() recovers the move with every tree done.
"""

from __future__ import annotations

from nadare.core import importmove
from nadare.core.config import Config


def _setup(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = Config.load(cfg_path)
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
    return cfg_path, library


def test_a_kill_between_the_moves_and_commit_can_still_be_finished(tmp_path):
    cfg_path, library = _setup(tmp_path)
    cfg = Config.load(cfg_path)
    importmove.run(importmove.plan(cfg, "g", library))
    # Killed here: the trees moved, the profile still names the old places.
    again = Config.load(cfg_path)
    move = importmove.plan(again, "g", library)
    assert set(move.done) == {"install_dir", "prefix"}, move
    importmove.run(move)
    importmove.commit(again, "g", move)
    again.save()
    home = library / "Nadare" / "Game"
    profile = Config.load(cfg_path).profiles["g"]
    assert profile["install_dir"].startswith(str(home)), profile
    assert profile["prefix"].startswith(str(home)), profile
    assert not importmove._journal_path(again).exists()
