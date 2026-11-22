# SPDX-License-Identifier: GPL-3.0-or-later
"""Move moves: after a verified copy to another disk, the original is deleted (user, 2026-11-14).

Before, Nadare kept the original and told the player to delete it ("the button is called
Move"). The original goes only once the profile points at the new copy, and only while the
new copy still matches it file for file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import relocate


def _tree(root: Path) -> Path:
    (root / "drive_c" / "windows").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    (root / "drive_c" / "game.exe").write_bytes(b"MZ" * 100)
    return root


def _copied(tmp_path: Path) -> tuple[Path, Path]:
    old = _tree(tmp_path / "home" / "prefix")
    new = tmp_path / "other" / "prefix"
    new.parent.mkdir(parents=True)
    import shutil

    shutil.copytree(old, new, symlinks=True)
    return old, new


def test_the_original_is_deleted_when_the_copy_matches(tmp_path):
    old, new = _copied(tmp_path)
    outcome = relocate.remove_original(old, new)
    assert outcome.removed and not old.exists()
    assert (new / "drive_c" / "game.exe").read_bytes() == b"MZ" * 100


def test_the_original_stays_when_the_copy_differs(tmp_path):
    old, new = _copied(tmp_path)
    (new / "drive_c" / "game.exe").write_bytes(b"MZ")
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and old.exists()
    assert "does not match" in outcome.reason


def test_the_original_stays_when_the_copy_is_gone(tmp_path):
    old, new = _copied(tmp_path)
    import shutil

    shutil.rmtree(new)
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and old.exists()


@pytest.mark.parametrize("relation", ["same", "new_inside_old", "old_inside_new"])
def test_overlapping_folders_are_never_deleted(tmp_path, relation):
    base = _tree(tmp_path / "game")
    if relation == "same":
        old, new = base, base
    elif relation == "new_inside_old":
        old, new = base, base / "drive_c"
    else:
        old, new = base / "drive_c", base
    outcome = relocate.remove_original(old, new)
    assert not outcome.removed and base.exists() and (base / "drive_c").exists()


def test_a_link_as_the_original_is_not_followed(tmp_path):
    old, new = _copied(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(old)
    outcome = relocate.remove_original(link, new)
    assert not outcome.removed and old.exists()


def test_a_real_cross_disk_move_result_is_accepted(tmp_path, monkeypatch):
    """A copy newer than the original (the game ran there) is accepted (#1366 A10)."""
    old = _tree(tmp_path / "home" / "prefix")
    new = tmp_path / "other" / "prefix"
    monkeypatch.setattr(relocate, "_same_device", lambda src, dst: False)
    moved = relocate.move_tree(old, new)
    assert moved.kept_old == old
    import os

    later = os.stat(old / "system.reg").st_mtime_ns + 60_000_000_000
    os.utime(new / "system.reg", ns=(later, later))
    assert relocate.remove_original(old, new).removed and not old.exists()
