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


def test_flatpak_names_the_flatpak_command(monkeypatch):
    monkeypatch.setattr(app_uninstall.sandbox, 'in_flatpak', lambda: True)
    text = app_uninstall.how_to_remove({'FLATPAK_ID': 'org.example.Nadare'})
    assert 'flatpak uninstall org.example.Nadare' in text
    assert 'Gear Lever' not in text


def test_an_appimage_nadare_may_not_replace_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app_uninstall.sandbox, 'in_flatpak', lambda: False)
    path = tmp_path / 'Nadare.AppImage'
    text = app_uninstall.how_to_remove({'APPIMAGE': str(path)})
    assert str(path) in text and 'Gear Lever' in text
    assert 'flatpak' not in text.lower()


def test_a_source_checkout_names_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(app_uninstall.sandbox, 'in_flatpak', lambda: False)
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(app_uninstall, '_source_root', lambda: tmp_path)
    text = app_uninstall.how_to_remove({})
    assert 'source checkout' in text and str(tmp_path) in text
    assert 'Gear Lever' not in text and 'flatpak' not in text.lower()


def test_a_package_install_points_at_the_package_manager(monkeypatch):
    monkeypatch.setattr(app_uninstall.sandbox, 'in_flatpak', lambda: False)
    monkeypatch.setattr(app_uninstall, '_source_root', lambda: None)
    text = app_uninstall.how_to_remove({})
    assert 'package manager' in text
