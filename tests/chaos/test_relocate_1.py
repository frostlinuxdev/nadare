# SPDX-License-Identifier: GPL-3.0-or-later
"""Interrupted/timing probe: a rename that says EXDEV must fall back to a checked copy.

A same-``st_dev`` rename can still return ``EXDEV`` (two mounts of one filesystem,
a bind mount, a btrfs subvolume).  The move code already has a verified copy path
for 'another disk'; it should use it instead of refusing the move.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from nadare.core import relocate


def _tree(root: Path) -> Path:
    (root / "drive_c" / "Game").mkdir(parents=True)
    (root / "drive_c" / "Game" / "game.bin").write_bytes(b"game" * 1000)
    (root / "system.reg").write_text("reg")
    return root


def test_a_rename_that_returns_exdev_still_moves_by_copying(tmp_path, monkeypatch):
    src = _tree(tmp_path / "src")
    dst = tmp_path / "other" / "dst"
    real_rename = os.rename

    def exdev_for_the_first_rename(a, b, *args, **kwargs):
        if Path(a) == src and Path(b) == dst:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b, *args, **kwargs)

    monkeypatch.setattr(relocate.os, "rename", exdev_for_the_first_rename)

    moved = relocate.move_tree(src, dst)

    print("moved:", moved)
    print("source still exists:", src.exists())
    print("destination file:", (dst / "drive_c" / "Game" / "game.bin").read_bytes()[:8])
    assert moved.renamed is False, "an EXDEV rename was not retried as a copy"
    assert moved.kept_old == src
    assert src.is_dir(), "the source must be kept when the move was a copy"
    assert (dst / "drive_c" / "Game" / "game.bin").read_bytes() == b"game" * 1000
