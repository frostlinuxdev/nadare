# SPDX-License-Identifier: GPL-3.0-or-later
"""Remember me is offered on the game page and the login QR card, not only in Settings."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import game_view
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    config.add_profile("plain", backend="wine", title="Plain")
    window = MainWindow(config)
    started: list[tuple] = []
    window.controller.start = lambda profile, op, payload=None: started.append(
        (profile, op, payload)
    ) or True
    window._started = started  # type: ignore[attr-defined]
    return window


def _remembers(path) -> bool:
    return bool(Config.load(path).profile("czn").get("remember_me", False))


def test_checkbox_only_for_backends_that_can_remember(tmp_path, qapp):
    window = _window(tmp_path)
    window.open_game("czn")
    assert not window.game.remember.isHidden()
    assert not window.game.remember.isChecked()
    window.open_game("plain")
    assert window.game.remember.isHidden()
    assert window.game.qr_frame.remember.isHidden()
    window.close()


def test_ticking_it_saves_the_flag_and_starts_set_remember(tmp_path, qapp):
    window = _window(tmp_path)
    window.open_game("czn")
    window._started.clear()
    window.game.remember.click()
    assert _remembers(tmp_path / "config.json")
    assert ("czn", "set_remember", True) in window._started
    assert window.game.qr_frame.remember.isChecked(), "the QR card box mirrors the page box"

    window._started.clear()
    window.game.remember.click()
    assert not _remembers(tmp_path / "config.json")
    assert ("czn", "set_remember", False) in window._started
    window.close()


def test_the_qr_card_checkbox_drives_the_same_setting(tmp_path, qapp):
    window = _window(tmp_path)
    window.open_game("czn")
    window._started.clear()
    window.game.qr_frame.remember.click()
    assert _remembers(tmp_path / "config.json")
    assert window.game.remember.isChecked()
    assert ("czn", "set_remember", True) in window._started
    window.close()


def test_the_box_reflects_the_saved_setting_when_the_page_opens(tmp_path, qapp):
    window = _window(tmp_path)
    with window.config.locked():
        window.config.profiles["czn"]["remember_me"] = True
        window.config.save()
    window.open_game("czn")
    assert window.game.remember.isChecked()
    assert window.game.qr_frame.remember.isChecked()
    window.close()


def test_a_failed_save_puts_the_box_back(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    window.open_game("czn")
    window._started.clear()
    shown: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.critical", lambda *args, **kw: shown.append(args[2])
    )
    monkeypatch.setattr(window.config, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    window.game.remember.click()
    assert not window.game.remember.isChecked()
    assert not window.config.profiles["czn"].get("remember_me", False)
    assert window._started == [] and shown
    window.close()


def test_page_has_the_shared_text(qapp):
    view = game_view.GameView()
    assert view.remember.text() == "Remember me on this device"
    assert "keyring" in view.remember.toolTip()
    view.deleteLater()


def test_no_keyring_turns_remember_me_back_off(tmp_path, qapp, monkeypatch):
    """With no usable keyring nothing can be saved: do not leave a ticked box lying."""
    from nadare.backends.czn.backend import REMEMBER_NOTICE

    window = _window(tmp_path)
    window.open_game("czn")
    monkeypatch.setenv("NADARE_NO_KEYRING", "1")
    shown: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.information", lambda *a, **k: shown.append(a[2])
    )
    window.game.remember.click()
    assert window.game.remember.isChecked() and _remembers(tmp_path / "config.json")
    window._remember_notice["czn"] = REMEMBER_NOTICE  # what the set_remember worker reports
    window._show_remember_notice("czn")
    assert not window.game.remember.isChecked()
    assert not window.game.qr_frame.remember.isChecked()
    assert not _remembers(tmp_path / "config.json")
    assert shown and "turned off" in shown[0] and "keyring" in shown[0]
    window.close()


def test_a_locked_keyring_keeps_the_choice(tmp_path, qapp, monkeypatch):
    """A transient failure with a working keyring must not discard what the user picked."""
    from nadare.backends.czn.backend import REMEMBER_NOTICE

    window = _window(tmp_path)
    window.open_game("czn")
    monkeypatch.delenv("NADARE_NO_KEYRING", raising=False)
    monkeypatch.setattr(
        "nadare.gui.main_window.secrets.SecretStore",
        lambda **kw: type("Store", (), {"secure": True})(),
    )
    monkeypatch.setattr("nadare.gui.main_window.QMessageBox.information", lambda *a, **k: None)
    window.game.remember.click()
    window._remember_notice["czn"] = REMEMBER_NOTICE
    window._show_remember_notice("czn")
    assert window.game.remember.isChecked() and _remembers(tmp_path / "config.json")
    window.close()


def test_a_remember_notice_from_login_is_shown_and_undoes_the_box(
    tmp_path, qapp, monkeypatch
):
    """F-03: login can discover the keyring is unusable; do not stay ticked."""
    from nadare.backends.czn.backend import REMEMBER_NOTICE
    from nadare.core.events import Event

    window = _window(tmp_path)
    window.open_game("czn")
    with window.config.locked():
        window.config.profiles["czn"]["remember_me"] = True
        window.config.save()
    shown: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.information", lambda *a, **k: shown.append(a[2])
    )
    monkeypatch.setattr(window, "_start", lambda *a, **k: True)

    window._on_event(
        "czn", Event("status", REMEMBER_NOTICE, data={"remember_unavailable": True})
    )
    window._on_succeeded("czn", "login", 0)

    assert shown and "turned off" in shown[0]
    assert not window.game.remember.isChecked()
    assert not _remembers(tmp_path / "config.json")
    window.close()
