# SPDX-License-Identifier: GPL-3.0-or-later
"""Make a file system hand a deleted folder's inode number to the next folder at that path.

ext4 does this all the time; tmpfs (``/tmp`` on many machines) never does, so a test that only
deletes and recreates a folder passes there and fails on GitHub's runners (b25 public CI). This
fixture makes the reuse certain on every file system: after ``reuse(path)``, ``os.stat``,
``os.lstat`` and ``os.fstat`` report the recorded device and inode for whatever folder is at
``path``, as ext4 may.

    def test_x(inode_reuse, tmp_path):
        inode_reuse.reuse(folder)      # remembers folder's (dev, ino)
        shutil.rmtree(folder); folder.mkdir()
        # folder now "is" the old one by device and inode
"""

from __future__ import annotations

import os

import pytest

_EXTRA = (
    "st_atime",
    "st_mtime",
    "st_ctime",
    "st_atime_ns",
    "st_mtime_ns",
    "st_ctime_ns",
    "st_blksize",
    "st_blocks",
    "st_rdev",
)


class InodeReuse:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._ids: dict[str, tuple[int, int]] = {}
        self._stat, self._lstat, self._fstat = os.stat, os.lstat, os.fstat
        monkeypatch.setattr(os, "stat", self._fake_stat)
        monkeypatch.setattr(os, "lstat", self._fake_lstat)
        monkeypatch.setattr(os, "fstat", self._fake_fstat)

    def reuse(self, path: os.PathLike | str) -> tuple[int, int]:
        """Pin ``path``'s current (dev, ino): any later folder there reports the same pair."""
        info = self._lstat(path)
        key = self._key(path)
        self._ids[key] = (info.st_dev, info.st_ino)
        return self._ids[key]

    @staticmethod
    def _key(path: os.PathLike | str) -> str:
        return os.path.normpath(os.path.abspath(os.fspath(path)))

    def _swap(self, key: str | None, info: os.stat_result) -> os.stat_result:
        pinned = self._ids.get(key) if key is not None else None
        if pinned is None or (info.st_mode & 0o170000) != 0o040000:
            return info
        seq = list(info[:10])
        seq[1], seq[2] = pinned[1], pinned[0]
        return os.stat_result(
            seq, {name: getattr(info, name) for name in _EXTRA if hasattr(info, name)}
        )

    def _path_key(self, path, dir_fd) -> str | None:
        if isinstance(path, int) or dir_fd is not None:
            return None
        return self._key(path)

    def _fake_stat(self, path, *, dir_fd=None, follow_symlinks=True):
        info = self._stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        return self._swap(self._path_key(path, dir_fd), info)

    def _fake_lstat(self, path, *, dir_fd=None):
        info = self._lstat(path, dir_fd=dir_fd)
        return self._swap(self._path_key(path, dir_fd), info)

    def _fake_fstat(self, fd):
        info = self._fstat(fd)
        try:
            key = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return info
        return self._swap(self._key(key), info)


@pytest.fixture
def inode_reuse():
    # Its own MonkeyPatch: a test that calls ``monkeypatch.undo()`` keeps the reuse.
    patch = pytest.MonkeyPatch()
    try:
        yield InodeReuse(patch)
    finally:
        patch.undo()
