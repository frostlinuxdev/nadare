# SPDX-License-Identifier: GPL-3.0-or-later
"""The GUI offers the FGO SELinux module removal only when it applies (#1555)."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core import fgo_selinux
from nadare.core.config import Config
from nadare.gui import main_window as mw
from nadare.gui import settings


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _settings_page(monkeypatch, available: bool):
    monkeypatch.setattr(fgo_selinux, "available", lambda: available)
    page = settings.SettingsPage()
    page.show()
    QThreadPool.globalInstance().waitForDone(5000)
    qapp = QApplication.instance()
    if qapp is not None:
        qapp.processEvents()
    return page


def test_the_checkbox_is_hidden_when_the_module_is_not_loaded(monkeypatch, qapp):
    page = _settings_page(monkeypatch, available=False)
    try:
        assert page.remove_fgo_selinux.isHidden()
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_the_checkbox_is_shown_and_unchecked_when_the_module_is_loaded(monkeypatch, qapp):
    page = _settings_page(monkeypatch, available=True)
    try:
        assert not page.remove_fgo_selinux.isHidden()
        assert page.remove_fgo_selinux.isChecked() is False
        assert page.remove_fgo_selinux.text() == (
            "Also remove the FGO SELinux module (asks for your password)"
        )
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def _uninstall_page(page, tmp_path, monkeypatch, *, checked: bool):
    path = tmp_path / "Nadare.AppImage"
    path.write_bytes(b"example")
    monkeypatch.setattr(settings.app_uninstall.selfupdate, "appimage_target", lambda: path)
    def move_to_trash(box):
        next(b for b in box.buttons() if b.text() == "Move to Trash").click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", move_to_trash)
    monkeypatch.setattr(settings.app_uninstall, "trash", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    page.remove_fgo_selinux.setVisible(True)
    page.remove_fgo_selinux.setChecked(checked)
    called: list[bool] = []
    on_gui_thread: list[bool] = []

    def remove(profile=None):
        on_gui_thread.append(threading.current_thread() is threading.main_thread())
        called.append(True)
        return True, "Removed."

    monkeypatch.setattr(fgo_selinux, "remove", remove)
    page.uninstall_nadare_button.click()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.instance().processEvents()
    page.selinux_on_gui_thread = on_gui_thread
    return called


def test_a_ticked_checkbox_removes_the_module_when_uninstalling(
    monkeypatch, tmp_path, qapp
):
    page = _settings_page(monkeypatch, available=True)
    try:
        assert _uninstall_page(page, tmp_path, monkeypatch, checked=True) == [True]
        # pkexec waits for the password; on the GUI thread the window would freeze.
        assert page.selinux_on_gui_thread == [False]
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_an_unticked_checkbox_leaves_selinux_alone(monkeypatch, tmp_path, qapp):
    page = _settings_page(monkeypatch, available=True)
    try:
        assert _uninstall_page(page, tmp_path, monkeypatch, checked=False) == []
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_removing_one_fgo_game_shows_the_command_and_does_not_run_it(
    monkeypatch, tmp_path, qapp
):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("wine", backend="wine", title="Wine game")
    cfg.add_profile("fgo", backend="fgo-lepton", title="FGO")
    window = mw.MainWindow(cfg)
    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    monkeypatch.setattr(
        fgo_selinux,
        "removal_command",
        lambda profile=None: "pkexec semodule -r lepton_binder",
    )
    ran: list[bool] = []
    monkeypatch.setattr(
        fgo_selinux,
        "remove",
        lambda profile=None: ran.append(True) or (True, "Removed."),
    )
    texts: list[str] = []
    real_set_text = QMessageBox.setInformativeText

    def spy(self, text):
        texts.append(text)
        real_set_text(self, text)

    monkeypatch.setattr(mw.QMessageBox, "setInformativeText", spy)
    monkeypatch.setattr(
        mw.QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel
    )
    try:
        window._on_remove_requested("fgo")
        QThreadPool.globalInstance().waitForDone(5000)
        qapp.processEvents()
        assert texts
        assert "pkexec semodule -r lepton_binder" in texts[0]
        assert ran == []
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_settings_checks_selinux_off_the_gui_thread(monkeypatch, qapp):
    threads = []

    def probe():
        threads.append(threading.current_thread() is threading.main_thread())
        return True

    monkeypatch.setattr(fgo_selinux, "available", probe)
    page = settings.SettingsPage()
    try:
        QThreadPool.globalInstance().waitForDone(5000)
        qapp.processEvents()
        assert threads == [False]
        assert not page.remove_fgo_selinux.isHidden()
    finally:
        page.close()
        page.deleteLater()


def test_game_summary_checks_selinux_off_the_gui_thread(monkeypatch, tmp_path, qapp):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("fgo", backend="fgo-lepton", title="FGO")
    window = mw.MainWindow(cfg)
    threads = []

    def probe():
        threads.append(threading.current_thread() is threading.main_thread())
        return True

    monkeypatch.setattr(fgo_selinux, "available", probe)
    monkeypatch.setattr(mw.QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)
    try:
        window._on_remove_requested("fgo")
        QThreadPool.globalInstance().waitForDone(5000)
        qapp.processEvents()
        assert threads == [False]
    finally:
        window.close()
        window.deleteLater()


def test_probe_result_after_page_deletion_is_ignored(monkeypatch, qapp):
    from PyQt6.QtCore import QEvent

    monkeypatch.setattr(fgo_selinux, "available", lambda: True)
    page = settings.SettingsPage()
    QThreadPool.globalInstance().waitForDone(5000)
    page.deleteLater()
    QApplication.sendPostedEvents(page, QEvent.Type.DeferredDelete)
    qapp.processEvents()
