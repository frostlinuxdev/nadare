# SPDX-License-Identifier: GPL-3.0-or-later
"""A build with missing backends must say so in the Store instead of just showing fewer games."""

from __future__ import annotations

import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork, backends
from nadare.gui.store import StorePage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *_args, **_kwargs: None)


def _shown_page(qapp) -> StorePage:
    page = StorePage()
    page.resize(900, 800)
    page.show()
    qapp.processEvents()
    return page


def test_missing_backends_show_one_banner_with_their_titles(monkeypatch, qapp, caplog):
    monkeypatch.setattr(backends, "unavailable", lambda: ["arknights-yostar", "stellasora-yostar"])
    with caplog.at_level(logging.WARNING, logger="nadare.gui.store"):
        page = _shown_page(qapp)
    try:
        banner = page.unavailable_banner
        assert banner.isVisible()
        text = banner.message_label.text()
        assert "2 games can't load in this build" in text
        assert "Arknights" in text
        assert "Stella Sora" in text
        assert banner.action_button.text() == "Open logs"
        assert banner.action_button.isVisible()
    finally:
        page.close()


def test_the_open_logs_action_opens_the_logs_dialog(monkeypatch, qapp):
    monkeypatch.setattr(backends, "unavailable", lambda: ["direct-bd2"])
    opened = []

    class FakeLogsDialog:
        def __init__(self, *args, **kwargs):
            opened.append((args, kwargs))

        def setAttribute(self, *_args):
            pass

        def show(self):
            opened.append("shown")

    monkeypatch.setattr("nadare.gui.store.LogsDialog", FakeLogsDialog)
    page = _shown_page(qapp)
    try:
        page.unavailable_banner.action_button.click()
        assert opened[0][0] == ([],)
        assert opened[1] == "shown"
    finally:
        page.close()


def test_no_banner_and_no_warning_when_nothing_is_missing(monkeypatch, qapp, caplog):
    monkeypatch.setattr(backends, "unavailable", lambda: [])
    with caplog.at_level(logging.WARNING, logger="nadare.gui.store"):
        page = _shown_page(qapp)
    try:
        assert page.unavailable_banner.isHidden()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    finally:
        page.close()


def test_a_deliberately_optional_kind_is_not_reported(monkeypatch, qapp, caplog):
    monkeypatch.setattr(backends, "unavailable", lambda: ["fgo-lepton"])
    with caplog.at_level(logging.WARNING, logger="nadare.gui.store"):
        page = _shown_page(qapp)
    try:
        assert page.unavailable_banner.isHidden()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    finally:
        page.close()


def test_each_missing_backend_gets_its_own_warning(monkeypatch, qapp, caplog):
    monkeypatch.setattr(backends, "unavailable", lambda: ["arknights-yostar", "stellasora-yostar"])
    with caplog.at_level(logging.WARNING, logger="nadare.gui.store"):
        page = _shown_page(qapp)
    try:
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        messages = " ".join(r.getMessage() for r in warnings)
        assert "arknights-yostar" in messages
        assert "stellasora-yostar" in messages
    finally:
        page.close()
