# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-6: one atomic small-file writer.

The copies disagreed: the Wine registry writers (netmarble, winebus) never fsynced, so a crash
could leave an empty ``user.reg``/``system.reg``; the cache writers used a pid-only temp name and
left it behind on failure; only some kept a symlinked config a symlink.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from nadare.core import atomicfile

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"


def test_writes_text_and_bytes(tmp_path):
    atomicfile.write(tmp_path / "a.txt", "héllo\r\n")
    atomicfile.write(tmp_path / "b.bin", b"\x00\xff")
    assert (tmp_path / "a.txt").read_bytes() == "héllo\r\n".encode()  # no newline translation
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\xff"


def test_surrogates_survive(tmp_path):
    text = b"key=\xff\xfe".decode("utf-8", "surrogateescape")  # a registry file read as such
    atomicfile.write(tmp_path / "user.reg", text)
    assert (tmp_path / "user.reg").read_bytes() == b"key=\xff\xfe"


def test_mode_explicit_kept_or_private(tmp_path):
    atomicfile.write(tmp_path / "x", "1", mode=0o644)
    assert (tmp_path / "x").stat().st_mode & 0o777 == 0o644
    atomicfile.write(tmp_path / "x", "2")  # an existing file keeps its mode
    assert (tmp_path / "x").stat().st_mode & 0o777 == 0o644
    atomicfile.write(tmp_path / "new", "3")  # a new file is private
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o600


def test_data_is_synced_before_the_rename(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(atomicfile.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(
        atomicfile.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
    )
    atomicfile.write(tmp_path / "f", "x")
    assert order.index("fsync") < order.index("replace")


def test_a_symlink_is_replaced_unless_followed(tmp_path):
    real = tmp_path / "real.vdf"
    real.write_text("old")
    link = tmp_path / "link.vdf"
    link.symlink_to(real)
    atomicfile.write(link, "followed", follow_symlink=True)
    assert link.is_symlink() and real.read_text() == "followed"
    atomicfile.write(link, "replaced")
    assert not link.is_symlink() and link.read_text() == "replaced"
    assert real.read_text() == "followed"


def test_a_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    folder = tmp_path / "w"  # tmp_path itself holds the test's fake home (conftest)
    folder.mkdir()
    target = folder / "f"
    target.write_text("old")

    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(atomicfile.os, "replace", boom)
    with pytest.raises(OSError):
        atomicfile.write(target, "new")
    assert target.read_text() == "old"
    assert sorted(p.name for p in folder.iterdir()) == ["f"]


def test_make_parent(tmp_path):
    atomicfile.write(tmp_path / "a" / "b" / "c.json", "{}", make_parent=True)
    assert (tmp_path / "a" / "b" / "c.json").read_text() == "{}"
    with pytest.raises(FileNotFoundError):
        atomicfile.write(tmp_path / "missing" / "c.json", "{}")


# The writers #1455 R4-6 names go through the helper (no private temp+rename left in them).
MIGRATED = [
    "core/netmarble.py",
    "core/winebus.py",
    "core/shortcuts.py",
    "core/logview.py",
    "core/steamtools.py",
    "core/journal.py",
    "core/indexfetch.py",
    "core/updatecheck.py",
    "core/notices.py",
]


@pytest.mark.parametrize("rel", MIGRATED)
def test_migrated_writers_have_no_private_copy(rel):
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name in ("_atomic_write", "_atomic_write_bytes", "_write_atomic", "_atomic_json")
    }
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in names | {"_write_cache"}:
            src = ast.unparse(fn)
            own = "mkstemp" in src or ".part" in src
            assert not own, f"{rel}:{fn.name} still rolls its own"
            assert "atomicfile.write" in src, f"{rel}:{fn.name} should call atomicfile.write"
