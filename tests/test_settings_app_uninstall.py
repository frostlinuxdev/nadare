# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from nadare.gui import settings


@pytest.fixture
def page():
    app = QApplication.instance() or QApplication([])
    widget = settings.SettingsPage()
    yield widget
    widget.deleteLater()
    app.processEvents()


def test_cancel_keeps_the_appimage(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    monkeypatch.setattr(settings.app_uninstall, 'trash', lambda *a: pytest.fail('cancelled'))
    page.uninstall_nadare_button.click()
    assert path.exists()


def test_confirmation_lists_exact_file_and_keeps_data(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    messages = []
    def confirm(*args):
        messages.append(args[2])
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, 'question', confirm)
    seen = []
    monkeypatch.setattr(settings.app_uninstall, 'trash', lambda plan, move: seen.append(plan.path))
    monkeypatch.setattr(QMessageBox, 'information', lambda *a: None)
    page.uninstall_nadare_button.click()
    assert seen == [path]
    assert str(path) in messages[0]
    assert 'games' in messages[0] and 'settings' in messages[0]


def test_other_installations_show_manager_instructions(page, monkeypatch):
    monkeypatch.setattr(settings.app_uninstall, 'preview', lambda: None)
    messages = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: messages.append(args[2]))
    page.uninstall_nadare_button.click()
    assert 'Flatpak' in messages[0] and 'Gear Lever' in messages[0]


def test_failed_trash_shows_error(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    def fail(*args):
        raise OSError('Trash unavailable')
    monkeypatch.setattr(settings.app_uninstall, 'trash', fail)
    messages = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: messages.append(args[2]))
    page.uninstall_nadare_button.click()
    assert messages == ['Trash unavailable']
    assert path.exists()
