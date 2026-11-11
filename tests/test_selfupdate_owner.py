# SPDX-License-Identifier: GPL-3.0-or-later
"""#811: self-update only replaces an AppImage this user owns, in a folder others cannot write,
and checks the download's hash again right before it goes into place."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nadare.core import selfupdate

NAME = "Nadare-0.1.0b30-x86_64.AppImage"


def _appimage(folder: Path) -> Path:
    path = folder / "Nadare-0.1.0b19-x86_64.AppImage"
    path.write_bytes(b"old")
    path.chmod(0o755)
    return path


def test_an_appimage_owned_by_someone_else_is_not_replaced(tmp_path, monkeypatch):
    path = _appimage(tmp_path)
    real = os.stat(path).st_uid
    monkeypatch.setattr(selfupdate.os, "getuid", lambda: real + 1)
    assert selfupdate.appimage_target({"APPIMAGE": str(path)}) is None


def test_a_world_writable_folder_without_the_sticky_bit_is_refused(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    path = _appimage(folder)
    folder.chmod(0o777)
    try:
        assert selfupdate.appimage_target({"APPIMAGE": str(path)}) is None
    finally:
        folder.chmod(0o755)


def test_a_sticky_world_writable_folder_is_fine(tmp_path):
    folder = tmp_path / "tmpish"
    folder.mkdir()
    path = _appimage(folder)
    folder.chmod(0o1777)
    try:
        assert selfupdate.appimage_target({"APPIMAGE": str(path)}) == path
    finally:
        folder.chmod(0o755)


def test_group_writable_is_fine_for_the_users_own_group_only(tmp_path, monkeypatch):
    folder = tmp_path / "grp"
    folder.mkdir()
    path = _appimage(folder)
    folder.chmod(0o775)
    gid = os.stat(folder).st_gid
    try:
        monkeypatch.setattr(selfupdate.os, "getgid", lambda: gid)
        assert selfupdate.appimage_target({"APPIMAGE": str(path)}) == path
        monkeypatch.setattr(selfupdate.os, "getgid", lambda: gid + 1)
        assert selfupdate.appimage_target({"APPIMAGE": str(path)}) is None
    finally:
        folder.chmod(0o755)


def test_a_download_changed_after_verifying_is_not_installed(tmp_path):
    target = _appimage(tmp_path)
    new = tmp_path / ".download"
    new.write_bytes(b"verified-bytes")
    sha = hashlib.sha256(b"verified-bytes").hexdigest()
    new.write_bytes(b"swapped-bytes")  # replaced between the check and the install
    with pytest.raises(selfupdate.SelfUpdateError):
        selfupdate.install_update(new, target, NAME, sha256=sha)
    assert target.read_bytes() == b"old" and not target.is_symlink()
    assert not (tmp_path / NAME).exists()


def test_the_verified_download_is_installed(tmp_path):
    target = _appimage(tmp_path)
    new = tmp_path / ".download"
    new.write_bytes(b"verified-bytes")
    sha = hashlib.sha256(b"verified-bytes").hexdigest()
    result = selfupdate.install_update(new, target, NAME, sha256=sha)
    assert result.read_bytes() == b"verified-bytes"


def test_the_gui_flow_passes_the_hash_to_the_install_step():
    text = Path("src/nadare/gui/selfupdate_flow.py").read_text(encoding="utf-8")
    assert "install_update(tmp, target, urls.filename, sha256=expected.sha256)" in text
