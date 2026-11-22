# SPDX-License-Identifier: GPL-3.0-or-later
"""The update's own backup is cleaned up by name, not found by a filename pattern (#1478).

``cleanup_previous`` looked for ``Nadare-*.AppImage.previous``: an AppImage the player renamed
(``custom.AppImage``) left its backup behind for good. The update now records the exact backup
it made, and the cleanup removes that file once the new version has started, nothing else.
"""

from __future__ import annotations

import json

from nadare.core import paths, selfupdate

NEW = "Nadare-0.1.0b25-x86_64.AppImage"


def _update(tmp_path, old_name: str, new_name: str = NEW):
    old = tmp_path / old_name
    old.write_bytes(b"old")
    incoming = tmp_path / ".incoming"
    incoming.write_bytes(b"new")
    dest = selfupdate.install_update(incoming, old, new_name)
    return old, dest, old.with_name(old.name + selfupdate.BACKUP_SUFFIX)


def _receipt():
    return paths.state_dir() / selfupdate.RECEIPT


def test_a_renamed_appimages_backup_goes_once_the_new_version_runs(tmp_path):
    _old, dest, backup = _update(tmp_path, "custom.AppImage")
    assert backup.exists()
    selfupdate.cleanup_previous(dest)
    assert not backup.exists()
    assert not _receipt().exists()
    assert dest.read_bytes() == b"new"


def test_nothing_goes_while_the_old_version_is_the_one_running(tmp_path):
    old, _dest, backup = _update(tmp_path, "custom.AppImage")
    selfupdate.cleanup_previous(tmp_path / "custom.AppImage.elsewhere")  # not the new file
    assert backup.exists() and _receipt().exists()


def test_a_same_name_update_keeps_its_backup_for_a_bad_update(tmp_path):
    """The running file's own backup stays (the existing policy)."""
    _old, dest, backup = _update(tmp_path, "custom.AppImage", "custom.AppImage")
    selfupdate.cleanup_previous(dest)
    assert backup.exists()


def test_a_receipt_naming_a_file_outside_the_folder_or_not_a_backup_is_ignored(tmp_path):
    _old, dest, _backup = _update(tmp_path, "custom.AppImage")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    victim = elsewhere / "notes.previous"
    victim.write_text("keep")
    plain = tmp_path / "notes.txt"
    plain.write_text("keep")
    for target in (victim, plain):
        _receipt().write_text(json.dumps({"backup": str(target), "new": str(dest)}))
        selfupdate.cleanup_previous(dest)
        assert target.exists()


def test_a_damaged_receipt_is_harmless(tmp_path):
    _old, dest, backup = _update(tmp_path, "custom.AppImage")
    _receipt().write_text("{damaged")
    selfupdate.cleanup_previous(dest)  # must not raise
    assert dest.exists()


def test_the_usual_versioned_name_still_cleans_up(tmp_path):
    _old, dest, backup = _update(tmp_path, "Nadare-0.1.0b24-x86_64.AppImage")
    selfupdate.cleanup_previous(dest)
    assert not backup.exists()


def test_starting_through_the_old_link_still_cleans_up(tmp_path):
    """A shortcut keeps the old path, which is now a link to the new file."""
    old, dest, backup = _update(tmp_path, "custom.AppImage")
    assert old.is_symlink()
    selfupdate.cleanup_previous(old)
    assert not backup.exists()
    assert dest.exists() and old.exists()
