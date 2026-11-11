# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Regressions for the seven core/backend findings of the 2026-11-08 review."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _backend_helpers import make_context

from nadare import app
from nadare.backends.direct import WutheringWavesBackend
from nadare.core import diskspace, shortcuts
from nadare.core.config import Config


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _join_cleanup_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "steam-shortcut-cleanup":
            thread.join(2)


def _build_window(qapp, config):
    from nadare.gui import main_window as mw

    window = mw.MainWindow(config)
    qapp.processEvents()
    return window


def _close_window(qapp, window) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("recovered", [True, False])
def test_startup_steam_cleanup_does_nothing_without_trustworthy_profiles(
    tmp_path, qapp, monkeypatch, recovered
):
    """A recovered config or an empty one must not look like "every game was removed"."""
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    if recovered:
        config.notice = "the settings file was unreadable and was reset"
    calls: list[tuple] = []
    monkeypatch.setattr(
        mw.shortcuts, "find_steam_shortcut_files", lambda: [tmp_path / "shortcuts.vdf"]
    )
    monkeypatch.setattr(
        mw.shortcuts,
        "refresh_steam_shortcuts",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    window = _build_window(qapp, config)
    try:
        _join_cleanup_threads()
        assert calls == []
    finally:
        _close_window(qapp, window)


def test_startup_steam_cleanup_still_runs_with_a_normal_config(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("kept", backend="wine", title="Kept", install_dir=str(tmp_path / "game"))
    calls: list[tuple] = []
    monkeypatch.setattr(
        mw.shortcuts, "find_steam_shortcut_files", lambda: [tmp_path / "shortcuts.vdf"]
    )
    monkeypatch.setattr(
        mw.shortcuts,
        "refresh_steam_shortcuts",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    window = _build_window(qapp, config)
    try:
        _join_cleanup_threads()
        assert calls, "the normal startup cleanup no longer runs"
    finally:
        _close_window(qapp, window)


def test_the_steam_running_message_does_not_promise_a_later_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    assert shortcuts.add_steam_shortcut(vdf, "work", "Work", ["/usr/bin/nadare"])
    monkeypatch.setattr(shortcuts, "steam_running", lambda: True)
    with pytest.raises(shortcuts.ShortcutError) as caught:
        shortcuts.remove_steam_shortcut(vdf, "work")
    text = str(caught.value)
    assert "close Steam and remove it again" in text
    assert "removes it the next time" not in text


def _wuwa_backend(tmp_path, quality: str) -> WutheringWavesBackend:
    values = {
        "install_dir": str(tmp_path / "wuwa"),
        "prefix": str(tmp_path / "prefix"),
        "wuwa_quality": quality,
    }
    return WutheringWavesBackend(make_context(tmp_path, "direct-wuwa", values))


def test_the_cli_space_check_uses_the_chosen_wuwa_tier(tmp_path, monkeypatch):
    backend = _wuwa_backend(tmp_path, "sd")
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 75 * 2**30)
    app._check_install_space(backend)  # SD fits; the old install_size_hint did not


def test_the_cli_space_check_refuses_when_the_chosen_uhd_tier_does_not_fit(
    tmp_path, monkeypatch
):
    backend = _wuwa_backend(tmp_path, "uhd")
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 95 * 2**30)
    with pytest.raises(Exception) as caught:
        app._check_install_space(backend)
    assert "space" in str(caught.value).lower() or "room" in str(caught.value).lower()
