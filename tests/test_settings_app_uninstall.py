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


def answer(monkeypatch, label, seen=None):
    """Answer the Move-to-Trash question by clicking the button named ``label``."""

    def exec_(box):
        if seen is not None:
            seen.append(box)
        for button in box.buttons():
            if button.text() == label:
                button.click()
                return 0
        pytest.fail(f'no {label!r} button in {[b.text() for b in box.buttons()]}')

    monkeypatch.setattr(QMessageBox, 'exec', exec_)


def test_the_question_names_its_buttons_and_defaults_to_cancel(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    monkeypatch.setattr(settings.app_uninstall, 'trash', lambda *a: pytest.fail('cancelled'))
    boxes = []
    answer(monkeypatch, 'Cancel', boxes)
    page.uninstall_nadare_button.click()
    labels = sorted(button.text() for button in boxes[0].buttons())
    assert labels == ['Cancel', 'Move to Trash']
    assert boxes[0].defaultButton().text() == 'Cancel'


def test_cancel_keeps_the_appimage(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    answer(monkeypatch, 'Cancel')
    monkeypatch.setattr(settings.app_uninstall, 'trash', lambda *a: pytest.fail('cancelled'))
    page.uninstall_nadare_button.click()
    assert path.exists()


def test_confirmation_lists_exact_file_and_keeps_data(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    boxes = []
    answer(monkeypatch, 'Move to Trash', boxes)
    messages = [lambda: boxes[0].text()]
    seen = []
    monkeypatch.setattr(settings.app_uninstall, 'trash', lambda plan, move: seen.append(plan.path))
    monkeypatch.setattr(QMessageBox, 'information', lambda *a: None)
    page.uninstall_nadare_button.click()
    assert seen == [path]
    text = messages[0]()
    assert str(path) in text
    assert 'games' in text and 'settings' in text


def test_other_installations_say_nothing_was_removed_and_the_one_step(page, monkeypatch):
    monkeypatch.setattr(settings.app_uninstall, 'preview', lambda: None)
    monkeypatch.setattr(settings.app_uninstall, 'how_to_remove', lambda: 'STEP FOR THIS INSTALL.')
    messages = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: messages.append(args[2]))
    page.uninstall_nadare_button.click()
    assert 'nothing was removed' in messages[0].lower()
    assert 'STEP FOR THIS INSTALL.' in messages[0]
    assert 'Gear Lever' not in messages[0]


def test_failed_trash_shows_error(page, tmp_path, monkeypatch):
    path = tmp_path / 'Nadare.AppImage'
    path.write_bytes(b'example')
    monkeypatch.setattr(settings.app_uninstall.selfupdate, 'appimage_target', lambda: path)
    answer(monkeypatch, 'Move to Trash')
    def fail(*args):
        raise OSError('Trash unavailable')
    monkeypatch.setattr(settings.app_uninstall, 'trash', fail)
    messages = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: messages.append(args[2]))
    page.uninstall_nadare_button.click()
    assert messages == ['Trash unavailable']
    assert path.exists()
