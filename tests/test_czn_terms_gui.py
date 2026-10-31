# SPDX-License-Identifier: GPL-3.0-or-later
"""The terms-of-service dialog (show, then agree on the user's click) and its hook (#795)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool, QUrl
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.backends.czn.backend import CznBackend
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.terms_dialog import TermsDialog
from nadare.gui.workers import BackendController

DOC = {
    "no": 1502,
    "id": "STOVE_CHAOSZEROSERVICE",
    "title": "[Chaos Zero Nightmare] Terms of Service",
    "html": "<h2>SMILEGATE TERMS</h2><p>You agree to everything.</p>",
    "required": True,
}


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone()
    qapp.processEvents()
    QThreadPool.globalInstance().waitForDone()
    qapp.processEvents()


def _dialog(qapp, load, accept=None, opened=None):
    calls = {"accept": 0}

    def do_accept():
        calls["accept"] += 1
        if accept is not None:
            accept()

    dialog = TermsDialog(
        "Chaos Zero Nightmare",
        load,
        do_accept,
        "https://policy.example/terms",
        open_url=(opened.append if opened is not None else (lambda url: None)),
    )
    return dialog, calls


def test_the_terms_are_shown_before_anything_can_be_agreed(qapp):
    dialog, calls = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]})
    try:
        assert not dialog.agree_button.isEnabled()  # nothing to read yet
        dialog.start()
        _settle(qapp)
        text = dialog.text.toPlainText()
        assert "Terms of Service" in text and "You agree to everything." in text
        assert dialog.agree_button.isEnabled()
        assert calls["accept"] == 0  # showing the terms agrees to nothing
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_clicking_agree_sends_the_agreement_once_and_closes(qapp):
    dialog, calls = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]})
    agreed: list[bool] = []
    dialog.agreed.connect(lambda: agreed.append(True))
    try:
        dialog.start()
        _settle(qapp)
        dialog.agree_button.click()
        assert not dialog.agree_button.isEnabled()  # a double click cannot send it twice
        dialog.agree_button.click()
        _settle(qapp)
        assert calls["accept"] == 1
        assert agreed == [True]
        assert dialog.result() == dialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_cancel_never_agrees(qapp):
    dialog, calls = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]})
    agreed: list[bool] = []
    dialog.agreed.connect(lambda: agreed.append(True))
    try:
        dialog.start()
        _settle(qapp)
        dialog.cancel_button.click()
        _settle(qapp)
        assert calls["accept"] == 0 and agreed == []
        assert dialog.result() == dialog.DialogCode.Rejected
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_failed_agreement_says_so_and_can_be_tried_again(qapp):
    def refuse():
        raise RuntimeError("STOVE said no")

    dialog, calls = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]}, refuse)
    agreed: list[bool] = []
    dialog.agreed.connect(lambda: agreed.append(True))
    try:
        dialog.start()
        _settle(qapp)
        dialog.agree_button.click()
        _settle(qapp)
        assert agreed == []
        assert "STOVE said no" in dialog.status.text()
        assert dialog.agree_button.isEnabled()
        assert dialog.result() != dialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_nothing_pending_means_nothing_to_agree_to(qapp):
    dialog, calls = _dialog(qapp, lambda: {"pending": False, "documents": []})
    try:
        dialog.start()
        _settle(qapp)
        assert not dialog.agree_button.isEnabled()
        assert "already" in dialog.status.text().lower()
        assert calls["accept"] == 0
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_failed_load_offers_the_browser_instead_of_agreeing(qapp):
    def broken():
        raise RuntimeError("no network")

    opened: list[str] = []
    dialog, calls = _dialog(qapp, broken, opened=opened)
    try:
        dialog.start()
        _settle(qapp)
        assert not dialog.agree_button.isEnabled()
        assert "no network" in dialog.status.text()
        dialog.browser_button.click()
        assert opened == ["https://policy.example/terms"]
        assert calls["accept"] == 0
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_terms_the_dialog_could_not_show_block_agreeing(qapp):
    dialog, calls = _dialog(
        qapp, lambda: {"pending": True, "documents": [DOC], "missing": [9999]}
    )
    try:
        dialog.start()
        _settle(qapp)
        assert not dialog.agree_button.isEnabled()
        assert "browser" in dialog.status.text().lower()
        assert calls["accept"] == 0
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_terms_text_can_load_nothing_from_outside(qapp):
    dialog, _ = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]})
    try:
        for kind in (1, 2, 3):  # html, image, style sheet
            assert dialog.text.loadResource(kind, QUrl("file:///etc/hostname")) in (None, b"")
            assert dialog.text.loadResource(kind, QUrl("https://evil.example/x.png")) in (None, b"")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_agree_is_never_the_default_button_so_enter_cannot_press_it(qapp):
    dialog, _ = _dialog(qapp, lambda: {"pending": True, "documents": [DOC]})
    try:
        assert not dialog.agree_button.autoDefault() and not dialog.agree_button.isDefault()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_dialog_cannot_be_closed_while_the_agreement_is_on_its_way(qapp):
    import threading

    release = threading.Event()
    dialog, calls = _dialog(
        qapp, lambda: {"pending": True, "documents": [DOC]}, lambda: release.wait(5)
    )
    agreed: list[bool] = []
    finished: list[int] = []
    dialog.agreed.connect(lambda: agreed.append(True))
    dialog.finished.connect(finished.append)
    try:
        dialog.start()
        _settle(qapp)
        dialog.agree_button.click()
        dialog.reject()  # Escape / the window's close button
        assert finished == []  # still open: the agreement is on its way
        release.set()
        _settle(qapp)
        assert agreed == [True]
        assert finished == [int(dialog.DialogCode.Accepted)]
    finally:
        release.set()
        dialog.deleteLater()
        qapp.processEvents()


def _czn_window(tmp_path, monkeypatch):
    # No real status probes: a CZN probe outlives the test and its follow-up work (runner
    # clean-up questions) would pop a modal box inside a later test.
    monkeypatch.setattr(BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    config.add_profile("other", backend="wine", title="Other")
    return MainWindow(config)


@pytest.mark.parametrize("operation", ["login", "launch"])
def test_a_terms_failure_for_czn_offers_the_terms(tmp_path, qapp, monkeypatch, operation):
    window = _czn_window(tmp_path, monkeypatch)
    offered: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_offer_terms", lambda p, op: offered.append((p, op)))
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    try:
        window._on_failed("czn", operation, CznBackend.TERMS_MESSAGE)
        assert offered == [("czn", operation)]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_other_failures_and_other_games_do_not_offer_the_terms(tmp_path, qapp, monkeypatch):
    window = _czn_window(tmp_path, monkeypatch)
    offered: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_offer_terms", lambda p, op: offered.append((p, op)))
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    try:
        window._on_failed("czn", "login", "STOVE returned an unexpected error")
        window._on_failed("czn", "install", CznBackend.TERMS_MESSAGE)
        window._on_failed("other", "login", CznBackend.TERMS_MESSAGE)
        assert offered == []
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_after_a_failed_login_the_user_is_told_they_are_already_logged_in(
    tmp_path, qapp, monkeypatch
):
    window = _czn_window(tmp_path, monkeypatch)
    notes: list[str] = []
    monkeypatch.setattr(window, "_set_side_status", notes.append)
    try:
        window._on_terms_agreed("czn", "login")
        window._on_terms_agreed("czn", "launch")
        assert "logged in" in notes[0].lower() and "log in again" not in notes[0].lower()
        assert "launch" in notes[1].lower()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_nothing_starts_after_the_window_is_closing(tmp_path, qapp, monkeypatch):
    window = _czn_window(tmp_path, monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(window, "_start", lambda *a, **k: started.append("x"))
    try:
        window._closing = True
        window._on_terms_agreed("czn", "login")
        assert started == []
    finally:
        window._closing = False
        window.close()
        window.deleteLater()
        qapp.processEvents()
