# SPDX-License-Identifier: GPL-3.0-or-later
"""After a self-update the path shortcuts were made with still starts Nadare (#869)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import selfupdate

OLD = "Nadare-0.1.0b19-x86_64.AppImage"
NEW = "Nadare-0.1.0b20-x86_64.AppImage"
NEWER = "Nadare-0.1.0b21-x86_64.AppImage"


def _file(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def _download(folder: Path, data: bytes) -> Path:
    return _file(folder, "download.AppImage", data)


def test_the_old_path_becomes_a_link_to_the_new_file(tmp_path):
    old = _file(tmp_path, OLD, b"old")
    dest = selfupdate.install_update(_download(tmp_path, b"new"), old, NEW)
    assert dest == tmp_path / NEW
    assert old.is_symlink() and old.resolve() == dest.resolve()
    assert old.read_bytes() == b"new"  # a shortcut Exec'ing the old path starts the new version
    assert os.access(old, os.X_OK)
    assert (tmp_path / (OLD + ".previous")).read_bytes() == b"old"


def test_the_link_is_relative_so_the_folder_can_move(tmp_path):
    old = _file(tmp_path, OLD, b"old")
    selfupdate.install_update(_download(tmp_path, b"new"), old, NEW)
    assert os.readlink(old) == NEW


def test_an_older_link_follows_the_next_update(tmp_path):
    first = _file(tmp_path, OLD, b"v19")
    second_path = selfupdate.install_update(_download(tmp_path, b"v20"), first, NEW)
    selfupdate.install_update(_download(tmp_path, b"v21"), second_path, NEWER)
    assert (tmp_path / OLD).read_bytes() == b"v21"
    assert (tmp_path / NEW).read_bytes() == b"v21"  # also a link now
    assert (tmp_path / NEWER).read_bytes() == b"v21"
    assert not (tmp_path / OLD).resolve().name.endswith(".previous")


def test_a_link_that_points_elsewhere_is_left_alone(tmp_path):
    other = _file(tmp_path, "Something-else.AppImage", b"other")
    (tmp_path / "Mine.AppImage").symlink_to(other.name)
    old = _file(tmp_path, OLD, b"old")
    selfupdate.install_update(_download(tmp_path, b"new"), old, NEW)
    assert (tmp_path / "Mine.AppImage").read_bytes() == b"other"


def test_an_update_that_keeps_the_file_name_makes_no_link(tmp_path):
    old = _file(tmp_path, NEW, b"old")
    dest = selfupdate.install_update(_download(tmp_path, b"new"), old, NEW)
    assert dest == old and not dest.is_symlink()
    assert dest.read_bytes() == b"new"


def test_a_folder_that_refuses_links_does_not_fail_the_update(tmp_path, monkeypatch):
    old = _file(tmp_path, OLD, b"old")

    def refuse(*args, **kwargs):
        raise OSError("symlinks are not allowed here")

    monkeypatch.setattr(os, "symlink", refuse)
    dest = selfupdate.install_update(_download(tmp_path, b"new"), old, NEW)
    assert dest.read_bytes() == b"new"
    assert not old.exists()


@pytest.mark.parametrize("target_is_link", [False, True])
def test_a_path_reached_through_a_link_still_updates(tmp_path, target_is_link):
    real = _file(tmp_path, OLD, b"old")
    target = real
    if target_is_link:
        (tmp_path / "launch.AppImage").symlink_to(real.name)
        target = tmp_path / "launch.AppImage"
    dest = selfupdate.install_update(_download(tmp_path, b"new"), target, NEW)
    assert dest.read_bytes() == b"new"
