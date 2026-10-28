# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings checks "is the game installed?" off the GUI thread and fails closed (#582)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path, name="sample", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend="stove-czn", prefix=str(tmp_path / "pfx"), **profile)
    return config


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


def _fake_backend(monkeypatch, status):
    seen: dict[str, object] = {}

    def create_backend(profile, config):
        def probe():
            seen["thread"] = threading.current_thread()
            return status() if callable(status) else status

        return SimpleNamespace(status=probe)

    monkeypatch.setattr(settings_mod, "create_backend", create_backend)
    return seen


def test_loading_settings_does_not_wait_for_the_probe(tmp_path, qapp, monkeypatch):
    seen = _fake_backend(
        monkeypatch, lambda: (time.sleep(1.0), SimpleNamespace(state="installed"))[1]
    )
    page = SettingsPage()
    try:
        started = time.monotonic()
        page.load(_config(tmp_path), "sample")
        assert time.monotonic() - started < 0.5
        assert page.option_widget("prefix").isReadOnly(), "locked while the check runs"
        _settle(qapp)
        assert seen["thread"] is not threading.main_thread()
        assert page.option_widget("prefix").isReadOnly()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_not_installed_answer_unlocks_the_prefix(tmp_path, qapp, monkeypatch):
    _fake_backend(monkeypatch, SimpleNamespace(state="not_installed"))
    page = SettingsPage()
    try:
        page.load(_config(tmp_path), "sample")
        _settle(qapp)
        assert not page.option_widget("prefix").isReadOnly()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_failed_probe_keeps_the_prefix_locked_and_says_so(tmp_path, qapp, monkeypatch):
    def boom():
        raise OSError("keyring is locked")

    _fake_backend(monkeypatch, boom)
    page = SettingsPage()
    try:
        page.load(_config(tmp_path), "sample")
        _settle(qapp)
        assert page.option_widget("prefix").isReadOnly()
        assert "could not check" in page._prefix_lock_note.text().lower()
        assert not page._prefix_lock_note.isHidden()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_late_answer_for_a_previous_profile_is_ignored(tmp_path, qapp, monkeypatch):
    answers = iter([SimpleNamespace(state="installed"), SimpleNamespace(state="not_installed")])
    gate = threading.Event()

    def status():
        answer = next(answers)
        if answer.state == "installed":
            gate.wait(5)
        return answer

    _fake_backend(monkeypatch, status)
    page = SettingsPage()
    try:
        config = _config(tmp_path)
        page.load(config, "sample")
        page.load(config, "sample")  # a second load while the first probe is still blocked
        gate.set()
        _settle(qapp)
        assert not page.option_widget("prefix").isReadOnly(), "only the newest answer counts"
    finally:
        gate.set()
        page.deleteLater()
        qapp.processEvents()


def test_a_background_task_that_finishes_after_its_signals_are_gone_does_not_raise(qapp):
    """At quit Qt deletes the signal object first; the worker thread must not abort on it."""
    from PyQt6 import sip

    from nadare.gui.workers import CallTask

    task = CallTask(lambda: 1)
    sip.delete(task.signals)
    task.run()  # must not raise RuntimeError("wrapped C/C++ object ... has been deleted")

    failing = CallTask(lambda: 1 / 0)
    sip.delete(failing.signals)
    failing.run()
