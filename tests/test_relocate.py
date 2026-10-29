# SPDX-License-Identifier: GPL-3.0-or-later
"""Moving a prefix to another place: rename on one disk, verified copy across disks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import relocate
from nadare.core.events import Cancel, Cancelled


def _tree(root: Path) -> Path:
    (root / "drive_c" / "Game").mkdir(parents=True)
    (root / "drive_c" / "Game" / "a.bin").write_bytes(b"a" * 3000)
    (root / "drive_c" / "b.txt").write_text("hello")
    (root / "dosdevices").mkdir()
    os.symlink("../drive_c", root / "dosdevices" / "c:")
    os.symlink("/", root / "dosdevices" / "z:")
    os.symlink("/nonexistent/target", root / "dangling")
    return root


def _snapshot(root: Path) -> dict[str, tuple]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            out[rel] = ("dir",)
        else:
            out[rel] = ("file", path.read_bytes())
    return out


@pytest.fixture
def cross_disk(monkeypatch):
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)


def test_same_disk_is_a_rename(tmp_path):
    src = _tree(tmp_path / "src")
    before = _snapshot(src)
    result = relocate.move_tree(src, tmp_path / "out" / "dst")
    assert result.renamed and result.kept_old is None
    assert not src.exists()
    assert _snapshot(tmp_path / "out" / "dst") == before


def test_cross_disk_copies_verifies_and_keeps_the_old_tree(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    before = _snapshot(src)
    seen = []
    result = relocate.move_tree(src, tmp_path / "dst", progress=lambda d, t: seen.append((d, t)))
    assert not result.renamed and result.kept_old == src
    assert _snapshot(src) == before  # untouched
    assert _snapshot(tmp_path / "dst") == before  # symlinks kept as links, even dangling
    assert not list(tmp_path.glob("*partial*"))
    assert seen and seen[-1][0] == seen[-1][1] > 0


@pytest.mark.parametrize("where", ["exists", "inside", "contains"])
def test_unsafe_destinations_are_refused(tmp_path, where):
    src = _tree(tmp_path / "src")
    dst = {
        "exists": tmp_path / "taken",
        "inside": src / "sub" / "dst",
        "contains": tmp_path,
    }[where]
    if where == "exists":
        dst.mkdir()
    with pytest.raises(relocate.RelocateError):
        relocate.move_tree(src, dst)
    assert _snapshot(src)  # nothing happened


def test_not_enough_space_changes_nothing(tmp_path, cross_disk, monkeypatch):
    src = _tree(tmp_path / "src")
    monkeypatch.setattr(relocate.diskspace, "free_bytes", lambda p: 10)
    with pytest.raises(relocate.RelocateError, match="space"):
        relocate.move_tree(src, tmp_path / "dst")
    assert not (tmp_path / "dst").exists() and not list(tmp_path.glob("*partial*"))


def test_cancel_removes_the_partial_copy_and_keeps_the_source(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    before = _snapshot(src)
    cancel = Cancel()
    with pytest.raises(Cancelled):
        relocate.move_tree(
            src, tmp_path / "dst", cancel=cancel, progress=lambda d, t: cancel.cancel()
        )
    assert not (tmp_path / "dst").exists() and not list(tmp_path.glob("*partial*"))
    assert _snapshot(src) == before


def test_a_copy_that_does_not_match_is_discarded(tmp_path, cross_disk, monkeypatch):
    src = _tree(tmp_path / "src")
    monkeypatch.setattr(
        relocate, "_summary", lambda root: (1, 2, 3, 4) if root == src else (0, 0, 0, 0)
    )
    with pytest.raises(relocate.RelocateError, match="verif"):
        relocate.move_tree(src, tmp_path / "dst")
    assert not (tmp_path / "dst").exists() and not list(tmp_path.glob("*partial*"))
    assert _snapshot(src)


def test_the_filesystem_root_and_home_are_never_moved(tmp_path):
    with pytest.raises(relocate.RelocateError):
        relocate.move_tree(Path.home(), tmp_path / "dst")


# ---- review fixes ------------------------------------------------------------
def test_a_read_only_subfolder_is_copied(tmp_path, cross_disk):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("x")
    (src / "sub").chmod(0o555)
    try:
        relocate.move_tree(src, tmp_path / "dst")
        assert (tmp_path / "dst" / "sub" / "f.txt").read_text() == "x"
        assert (tmp_path / "dst" / "sub").stat().st_mode & 0o777 == 0o555  # mode restored last
    finally:
        (src / "sub").chmod(0o755)
        if (tmp_path / "dst" / "sub").exists():
            (tmp_path / "dst" / "sub").chmod(0o755)


def test_a_file_changed_while_copying_is_caught(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    big = src / "drive_c" / "Game" / "a.bin"
    for old_file in src.rglob("*"):  # the kernel's coarse clock could tie a fresh write
        if old_file.is_file() and not old_file.is_symlink():
            os.utime(old_file, (1_000_000, 1_000_000))
    state = {"done": False}

    def meddle(done, total):
        if not state["done"]:
            state["done"] = True
            big.write_bytes(b"b" * 3000)  # same size, new content and mtime

    with pytest.raises(relocate.RelocateError, match="changed"):
        relocate.move_tree(src, tmp_path / "dst", progress=meddle)
    assert not (tmp_path / "dst").exists() and not list(tmp_path.glob("*partial*"))


def test_a_fifo_is_skipped_instead_of_hanging(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    os.mkfifo(src / "pipe")
    relocate.move_tree(src, tmp_path / "dst")
    assert not (tmp_path / "dst" / "pipe").exists()
    assert (tmp_path / "dst" / "drive_c" / "b.txt").read_text() == "hello"


def test_a_folder_someone_else_made_at_the_partial_name_is_never_deleted(
    tmp_path, cross_disk, monkeypatch
):
    src = _tree(tmp_path / "src")
    foreign = tmp_path / ("dst" + relocate.PARTIAL_SUFFIX)

    def sneaky(path):
        foreign.mkdir()
        (foreign / "theirs.txt").write_text("keep me")
        return 10 << 30

    monkeypatch.setattr(relocate.diskspace, "free_bytes", sneaky)
    with pytest.raises(relocate.RelocateError):
        relocate.move_tree(src, tmp_path / "dst")
    assert (foreign / "theirs.txt").read_text() == "keep me"


def test_a_destination_that_appears_before_the_rename_is_not_replaced(tmp_path, monkeypatch):
    src = _tree(tmp_path / "src")
    dst = tmp_path / "dst"

    def appear(a, b):
        dst.mkdir()
        return True

    monkeypatch.setattr(relocate, "_same_device", appear)
    with pytest.raises(relocate.RelocateError, match="appeared"):
        relocate.move_tree(src, dst)
    assert src.is_dir() and not list(dst.iterdir())


# ---- edge-case audit (b17) ---------------------------------------------------
@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 000 directory")
def test_an_unreadable_subfolder_stops_the_move_instead_of_being_skipped(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    locked = src / "locked"
    locked.mkdir()
    (locked / "precious.bin").write_bytes(b"x" * 100)
    locked.chmod(0o000)
    try:
        with pytest.raises(relocate.RelocateError):
            relocate.move_tree(src, tmp_path / "dst")
        assert not (tmp_path / "dst").exists() and not list(tmp_path.glob("*partial*"))
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 000 directory")
def test_an_unreadable_source_root_is_refused_not_copied_empty(tmp_path, cross_disk):
    src = _tree(tmp_path / "src")
    src.chmod(0o000)
    try:
        with pytest.raises(relocate.RelocateError):
            relocate.move_tree(src, tmp_path / "dst")
        assert not (tmp_path / "dst").exists()
    finally:
        src.chmod(0o755)


def test_a_failed_copy_with_a_read_only_folder_leaves_no_partial_behind(
    tmp_path, cross_disk, monkeypatch
):
    src = _tree(tmp_path / "src")
    (src / "ro").mkdir()
    (src / "ro" / "keep.txt").write_text("x")
    (src / "ro").chmod(0o555)
    real_stat = relocate.shutil.copystat

    def failing_stat(a, b, **kw):
        if str(a).endswith("src"):  # the last pass, after the read-only mode was applied
            raise OSError("vanished")
        return real_stat(a, b, **kw)

    monkeypatch.setattr(relocate.shutil, "copystat", failing_stat)
    try:
        with pytest.raises(relocate.RelocateError):
            relocate.move_tree(src, tmp_path / "dst")
        assert not list(tmp_path.glob("*partial*"))
    finally:
        (src / "ro").chmod(0o755)
