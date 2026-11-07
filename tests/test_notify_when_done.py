# SPDX-License-Identifier: GPL-3.0-or-later
"""A desktop notification when an install, update or repair ends while the window is not in use."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import notify
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notify, "send", lambda summary, body="": calls.append((summary, body)) or True
    )
    return calls


def _window(tmp_path, **settings):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.data.update(settings)
    config.add_profile("wuwa", backend="wine", title="Wuthering Waves")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    return window


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


# ---- the words ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("operation", "ok", "summary"),
    [
        ("install", True, "Wuthering Waves is installed"),
        ("update", True, "Wuthering Waves is updated"),
        ("repair", True, "Wuthering Waves is repaired"),
        ("install", False, "Wuthering Waves could not be installed"),
        ("update", False, "Wuthering Waves could not be updated"),
        ("repair", False, "Wuthering Waves could not be repaired"),
    ],
)
def test_the_notification_names_the_game_and_what_happened(operation, ok, summary):
    assert notify.finished_text(operation, "Wuthering Waves", ok)[0] == summary


@pytest.mark.parametrize(
    "operation", ["launch", "status", "news", "media", "check_update", "login"]
)
def test_other_operations_have_no_notification(operation):
    assert notify.finished_text(operation, "Game", True) is None
    assert notify.finished_text(operation, "Game", False) is None


def test_send_never_raises_without_a_notification_service(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    assert notify.send("hello", "body") in (True, False)


# ---- the window ---------------------------------------------------------------------------
def test_a_finished_install_in_the_background_notifies(tmp_path, qapp, sent):
    window = _window(tmp_path)
    try:
        window._on_succeeded("wuwa", "install", 0)
        assert [s for s, _b in sent] == ["Wuthering Waves is installed"]
    finally:
        _close(window, qapp)


def test_a_failed_update_notifies_too(tmp_path, qapp, sent):
    window = _window(tmp_path)
    try:
        window._on_failed("wuwa", "update", "disk full")
        assert [s for s, _b in sent] == ["Wuthering Waves could not be updated"]
    finally:
        _close(window, qapp)


def test_nothing_is_sent_while_the_window_is_in_use(tmp_path, qapp, sent, monkeypatch):
    window = _window(tmp_path)
    try:
        monkeypatch.setattr(window, "isActiveWindow", lambda: True)
        window._on_succeeded("wuwa", "install", 0)
        assert sent == []
    finally:
        _close(window, qapp)


def test_the_setting_turns_it_off(tmp_path, qapp, sent):
    window = _window(tmp_path, notify_when_done=False)
    try:
        window._on_succeeded("wuwa", "install", 0)
        window._on_failed("wuwa", "install", "x")
        assert sent == []
    finally:
        _close(window, qapp)


def test_playing_a_game_sends_nothing(tmp_path, qapp, sent):
    window = _window(tmp_path)
    try:
        window._on_succeeded("wuwa", "launch", 0)
        assert sent == []
    finally:
        _close(window, qapp)


def test_a_cancelled_install_sends_nothing(tmp_path, qapp, sent):
    window = _window(tmp_path)
    try:
        window._on_cancelled("wuwa", "install")
        assert sent == []
    finally:
        _close(window, qapp)


# ---- Settings -----------------------------------------------------------------------------
def test_the_default_is_on_and_settings_save_it(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.open_settings()
        page = window.settings
        assert page.notify_when_done.isChecked()
        page.notify_when_done.setChecked(False)
        page.save()
        assert window.config.data["notify_when_done"] is False
        assert Config.load(tmp_path / "config.json").data["notify_when_done"] is False
    finally:
        _close(window, qapp)
