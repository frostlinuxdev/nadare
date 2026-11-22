# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage review round 2 (opencode-go Flash, 2026-11-14): Move's delete of the original.

C1: the copy was not flushed to disk before the original was deleted.
C2: a link reaching the original through another route (``/home`` vs ``/var/home``) passed.
C3: the dialogs promised a delete that does not happen for a player's own folders.
C4: a crash after the profile was saved left the original with no record to finish it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from nadare.core import libraries, relocate
from nadare.core.config import Config


def _prefix(root: Path) -> Path:
    (root / "drive_c" / "windows").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def _copy(old: Path, new: Path) -> None:
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old, new, symlinks=True)


# ---- C1 -------------------------------------------------------------------------------------
def test_the_copy_is_flushed_to_disk_before_the_original_goes(tmp_path, monkeypatch):
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    _copy(old, new)
    order: list[str] = []
    monkeypatch.setattr(relocate.os, "sync", lambda: order.append("sync"))
    from nadare.core import removal

    real = removal._rmtree_safely

    def rm(path, *a, **k):
        order.append("delete")
        return real(path, *a, **k)

    monkeypatch.setattr(removal, "_rmtree_safely", rm)
    assert relocate.remove_original(old, new).removed
    assert order == ["sync", "delete"]


# ---- C2 -------------------------------------------------------------------------------------
def test_a_link_into_the_original_by_another_route_keeps_it(tmp_path):
    real = tmp_path / "real"
    old = _prefix(real / "prefix")
    route = tmp_path / "route"
    route.symlink_to(real)  # /home -> /var/home, say
    (old / "alias").symlink_to(route / "prefix" / "drive_c")
    new = tmp_path / "b" / "prefix"
    _copy(old, new)
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and old.is_dir()


def test_a_relative_link_that_climbs_back_into_the_original_keeps_it(tmp_path):
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    # From b/prefix/x: ../../a/prefix/drive_c is the original's drive_c.
    os.symlink("../../a/prefix/drive_c", old / "x")
    _copy(old, new)
    assert not relocate.remove_original(old, new).removed and old.is_dir()


# ---- C4 -------------------------------------------------------------------------------------
def test_a_pending_original_is_saved_with_the_new_location(tmp_path):
    old, new = tmp_path / "a" / "prefix", tmp_path / "b" / "prefix"
    profile = {"prefix": str(old)}
    changes = relocate.with_pending_original({"prefix": str(new)}, profile, old, new)
    assert changes[relocate.PENDING_ORIGINALS] == [[str(old), str(new)]]
    profile.update(changes)
    again = relocate.with_pending_original({}, profile, tmp_path / "c", tmp_path / "d")
    assert len(again[relocate.PENDING_ORIGINALS]) == 2


def test_startup_forgets_a_pending_original_that_is_gone(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    new = _prefix(tmp_path / "b" / "prefix")
    cfg.add_profile(
        "bd2",
        backend="direct-bd2",
        title="BD2",
        prefix=str(new),
        **{relocate.PENDING_ORIGINALS: [[str(tmp_path / "gone"), str(new)]]},
    )
    assert libraries.backfill(cfg) is True
    assert relocate.PENDING_ORIGINALS not in cfg.profiles["bd2"]


def test_only_records_whose_copy_is_where_the_game_lives_are_offered(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    old = _prefix(tmp_path / "a" / "prefix")
    new = tmp_path / "b" / "prefix"
    _copy(old, new)
    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    cfg.add_profile(
        "bd2",
        backend="direct-bd2",
        title="BD2",
        prefix=str(new),
        **{
            relocate.PENDING_ORIGINALS: [
                relocate.with_pending_original({}, {}, old, new)[relocate.PENDING_ORIGINALS][0],
                [str(elsewhere), str(tmp_path / "not-the-game")],  # hand-edited
            ]
        },
    )
    assert relocate.pending_originals(cfg) == [("bd2", old, new)]
