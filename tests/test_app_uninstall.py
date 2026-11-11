# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from nadare.core import app_uninstall


def image(tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'fake application')
    monkeypatch.setattr(app_uninstall.selfupdate, 'appimage_target', lambda: path)
    return path


def test_preview_and_trash_only_the_application(tmp_path, monkeypatch):
    path = image(tmp_path, monkeypatch)
    game = tmp_path / 'game-save'
    game.write_text('keep')
    plan = app_uninstall.preview()
    seen = []
    app_uninstall.trash(plan, lambda p: (seen.append(p) or True, 'trash/file'))
    assert seen == [str(path)]
    assert game.read_text() == 'keep'


def test_managed_or_source_install_has_no_plan(monkeypatch):
    monkeypatch.setattr(app_uninstall.selfupdate, 'appimage_target', lambda: None)
    assert app_uninstall.preview() is None


def test_symlink_is_refused(tmp_path, monkeypatch):
    path = image(tmp_path, monkeypatch)
    other = tmp_path / 'other'
    path.rename(other)
    path.symlink_to(other)
    assert app_uninstall.preview() is None


def test_replaced_file_is_refused_after_confirmation(tmp_path, monkeypatch):
    path = image(tmp_path, monkeypatch)
    plan = app_uninstall.preview()
    path.rename(tmp_path / 'original')
    path.write_bytes(b'replacement')
    with pytest.raises(ValueError, match='changed'):
        app_uninstall.trash(plan, lambda p: pytest.fail('must not trash'))


def test_trash_failure_never_falls_back_to_delete(tmp_path, monkeypatch):
    path = image(tmp_path, monkeypatch)
    with pytest.raises(OSError, match='Trash'):
        app_uninstall.trash(app_uninstall.preview(), lambda p: (False, ''))
    assert path.exists()


def test_missing_file_is_reported(tmp_path, monkeypatch):
    path = image(tmp_path, monkeypatch)
    plan = app_uninstall.preview()
    path.rename(tmp_path / 'moved')
    with pytest.raises(OSError):
        app_uninstall.trash(plan, lambda p: pytest.fail('must not trash'))
