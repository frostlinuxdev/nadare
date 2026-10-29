# SPDX-License-Identifier: GPL-3.0-or-later
"""Empty members are staged too, and per-open staging names may not collide."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import sevenz

FIX = Path(__file__).parent / "fixtures" / "sevenz"


def open7z(name: str):
    return open(FIX / f"{name}.7z", "rb")


def assert_regular(path: Path) -> None:
    assert path.is_file()
    assert not path.is_symlink()


def test_empty_member_is_staged_and_ignores_a_planted_staging_symlink(tmp_path):
    dest = tmp_path / "install"
    dest.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"KEEP")
    (dest / "empty.bin.nadare-new").symlink_to(victim)

    with open7z("lzma2") as fh:
        sevenz.extract(fh, dest, only=lambda path: path == "empty.bin")

    assert victim.read_bytes() == b"KEEP"
    assert_regular(dest / "empty.bin")
    assert (dest / "empty.bin").read_bytes() == b""
    assert not list(dest.rglob("*.nadare-new"))


def test_empty_member_still_refuses_a_planted_final_path_symlink(tmp_path):
    dest = tmp_path / "install"
    dest.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"KEEP")
    (dest / "empty.bin").symlink_to(victim)

    with open7z("lzma2") as fh, pytest.raises(sevenz.SevenZipError, match="symlink"):
        sevenz.extract(fh, dest, only=lambda path: path == "empty.bin")

    assert victim.read_bytes() == b"KEEP"


def test_two_staging_opens_for_one_final_path_do_not_collide(tmp_path):
    dest = tmp_path / "install"
    final1, active1 = sevenz._staged_path(dest, "dup.bin")
    final2, active2 = sevenz._staged_path(dest, "dup.bin")

    assert final1 == final2
    assert active1 != active2, "each staging open needs its own name"
    assert active1.name.endswith(".nadare-new")
    assert active2.name.endswith(".nadare-new")

    with sevenz._open_staging(active1) as first:
        with sevenz._open_staging(active2) as second:
            first.write(b"first")
            second.write(b"second")

    os.replace(active1, final1)
    os.replace(active2, final2)
    assert final1.read_bytes() == b"second"
    assert not list(dest.rglob("*.nadare-new"))


def test_extraction_leaves_no_staging_files_behind(tmp_path):
    with open7z("lzma2") as fh:
        sevenz.extract(fh, tmp_path)
    assert not list(tmp_path.rglob("*.nadare-new"))


def test_staging_leftovers_of_a_dead_process_are_swept_but_a_live_ones_are_kept(tmp_path):
    """A crash used to be self-healing (the fixed name was overwritten next run); with
    unique names the orphans of a dead process must be removed, a live process's kept."""
    import os

    from nadare.core import sevenz

    (tmp_path / "sub").mkdir()
    dead = tmp_path / "sub" / ".7ffffff0-abcdef.nadare-new"  # pid 0x7ffffff0: not alive
    live = tmp_path / "sub" / f".{os.getpid():x}-123456.nadare-new"
    other = tmp_path / "sub" / "game.bin.keep"  # not a staging name
    for p in (dead, live, other):
        p.write_bytes(b"x")

    final, active = sevenz._staged_path(tmp_path, "sub/game.bin")

    assert not dead.exists(), "an orphan of a dead process must be swept"
    assert live.exists(), "a staging file of this live process must stay"
    assert other.exists()
    assert active.name.endswith(".nadare-new") and active.parent == final.parent


def test_the_directory_is_swept_once_not_for_every_member(tmp_path):
    """A folder with thousands of members must not be rescanned per member (O(n^2))."""
    from nadare.core import sevenz

    (tmp_path / "sub").mkdir()
    sevenz._staged_path(tmp_path, "sub/a.bin")
    late = tmp_path / "sub" / ".7ffffff0-abcdef.nadare-new"
    late.write_bytes(b"x")
    sevenz._staged_path(tmp_path, "sub/b.bin")
    assert late.exists(), "second member in the same directory must not rescan it"


def test_a_long_member_name_still_fits_the_staging_name(tmp_path):
    from nadare.core import sevenz

    name = "x" * 240 + ".bin"  # fits NAME_MAX (255) on its own
    final, active = sevenz._staged_path(tmp_path, name)
    with sevenz._open_staging(active) as handle:
        handle.write(b"ok")
    os.replace(active, final)
    assert final.read_bytes() == b"ok"
