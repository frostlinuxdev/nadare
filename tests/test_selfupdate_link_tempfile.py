# SPDX-License-Identifier: GPL-3.0-or-later
"""Updating must not consume an unrelated file named like a temporary shortcut."""

from nadare.core import selfupdate


def test_update_preserves_a_user_file_next_to_the_old_appimage(tmp_path):
    old = tmp_path / "Nadare-old.AppImage"
    old.write_bytes(b"old application")
    notes = tmp_path / "Nadare-old.AppImage.link"
    notes.write_bytes(b"user-owned link or notes")
    download = tmp_path / "download"
    download.write_bytes(b"new application")
    new = selfupdate.install_update(download, old, "Nadare-new.AppImage")
    assert notes.read_bytes() == b"user-owned link or notes"
    assert old.is_symlink() and old.resolve() == new


def test_update_preserves_user_files_next_to_earlier_shortcut_links(tmp_path):
    old = tmp_path / "Nadare-old.AppImage"
    old.write_bytes(b"old application")
    earlier = tmp_path / "Nadare-earlier.AppImage"
    earlier.symlink_to(old.name)
    notes = tmp_path / "Nadare-earlier.AppImage.link"
    notes.write_bytes(b"keep this too")
    download = tmp_path / "download"
    download.write_bytes(b"new application")
    new = selfupdate.install_update(download, old, "Nadare-new.AppImage")
    assert notes.read_bytes() == b"keep this too"
    assert earlier.resolve() == new
