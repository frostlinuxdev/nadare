# SPDX-License-Identifier: GPL-3.0-or-later
"""#771: the update banner updates the AppImage in place, asks before restarting, falls back."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork, selfupdate
from nadare.core.config import Config
from nadare.core.updatecheck import UpdateInfo
from nadare.gui import main_window as mw_mod
from nadare.gui.main_window import MainWindow

#: The fakes below "download" these bytes; the install step checks their hash again (#811).
NEW_SHA = hashlib.sha256(b"new-bytes").hexdigest()

INFO = UpdateInfo(
    version="0.1.0b20",
    url="https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20",
    name="Nadare 0.1.0b20",
)
NAME = "Nadare-0.1.0b20-x86_64.AppImage"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _setup(tmp_path: Path, monkeypatch, *, updatable: bool = True, fail: str | None = None):
    target = tmp_path / "apps" / "Nadare-0.1.0b19-x86_64.AppImage"
    target.parent.mkdir()
    target.write_bytes(b"old")
    seen = SimpleNamespace(restarted=[], downloads=0, quit=0, messages=[], opened=[])

    def download_verified(url, dest, sha256, **kwargs):
        seen.downloads += 1
        if fail:
            raise selfupdate.SelfUpdateError(fail)
        dest.write_bytes(b"new-bytes")

    monkeypatch.setattr(
        selfupdate, "appimage_target", lambda *a, **k: target if updatable else None
    )
    monkeypatch.setattr(
        selfupdate,
        "expected_sha256",
        lambda info, **k: SimpleNamespace(sha256=NEW_SHA, source="index"),
    )
    monkeypatch.setattr(selfupdate, "download_verified", download_verified)
    # The release signature is tested in test_release_signature.py / test_selfupdate.py.
    monkeypatch.setattr(selfupdate, "verify_signature", lambda urls, path: None)
    monkeypatch.setattr(selfupdate, "restart", seen.restarted.append)
    monkeypatch.setattr(
        mw_mod.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: seen.opened.append(url.toString())),
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="direct-pgr", title="A")
    config.save()
    window = MainWindow(config)
    window._close_window_for_update = lambda: True
    window._quit_app = lambda: setattr(seen, "quit", seen.quit + 1)
    window._ask_restart = lambda version: seen.messages.append(("ask", version)) or False
    window._show_update_failure = lambda message: seen.messages.append(("failed", message))
    return window, target, seen


def _close(window, qapp) -> None:
    window.controller._workers.clear()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_banner_offers_update_when_the_appimage_can_replace_itself(
    tmp_path, qapp, monkeypatch
):
    window, _target, _seen = _setup(tmp_path, monkeypatch)
    try:
        window._show_update(INFO)
        assert window.update_banner.action_button.text() == "Update"
    finally:
        _close(window, qapp)


def test_the_banner_offers_download_when_it_cannot(tmp_path, qapp, monkeypatch):
    window, _target, _seen = _setup(tmp_path, monkeypatch, updatable=False)
    try:
        window._show_update(INFO)
        assert window.update_banner.action_button.text() == "Download"
        window.update_banner.action_button.click()
        assert _seen.opened == [INFO.url]
    finally:
        _close(window, qapp)


def test_pressing_update_installs_the_new_file_and_asks_before_restarting(
    tmp_path, qapp, monkeypatch
):
    window, target, seen = _setup(tmp_path, monkeypatch)
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: ("ask", "0.1.0b20") in seen.messages)
        new = target.parent / NAME
        assert new.read_bytes() == b"new-bytes"
        assert (target.parent / (target.name + ".previous")).read_bytes() == b"old"
        assert seen.restarted == [] and seen.quit == 0  # asked, answered "later": nothing restarts
        assert window.update_banner.action_button.text() == "Restart"
    finally:
        _close(window, qapp)


def test_restarting_starts_the_new_file_and_quits(tmp_path, qapp, monkeypatch):
    window, target, seen = _setup(tmp_path, monkeypatch)
    window._ask_restart = lambda version: True
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: bool(seen.restarted))
        assert seen.restarted == [target.parent / NAME]
        assert seen.quit == 1
    finally:
        _close(window, qapp)


def test_the_restart_banner_button_restarts_later_on_request(tmp_path, qapp, monkeypatch):
    window, target, seen = _setup(tmp_path, monkeypatch)
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: window.update_banner.action_button.text() == "Restart")
        window.update_banner.action_button.click()  # "Restart" pressed: ask again, say yes
        window._ask_restart = lambda version: True
        window.update_banner.action_button.click()
        assert seen.restarted == [target.parent / NAME]
    finally:
        _close(window, qapp)


def test_restart_waits_while_an_install_is_running(tmp_path, qapp, monkeypatch):
    window, _target, seen = _setup(tmp_path, monkeypatch)
    window._ask_restart = lambda version: True
    monkeypatch.setattr(window.controller, "mutating_profiles", lambda: ["a"])
    window._inform_busy_restart = lambda: seen.messages.append(("busy",))
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: ("busy",) in seen.messages)
        assert seen.restarted == [] and seen.quit == 0
    finally:
        # Closing would ask to confirm quitting while an install "runs".
        monkeypatch.setattr(window.controller, "mutating_profiles", lambda: [])
        _close(window, qapp)


def test_restart_waits_while_a_game_is_being_removed(tmp_path, qapp, monkeypatch):
    window, _target, seen = _setup(tmp_path, monkeypatch)
    window._ask_restart = lambda version: True
    window._inform_busy_restart = lambda: seen.messages.append(("busy",))
    try:
        window._show_update(INFO)
        window._busy["a"] = "remove"  # a removal runs on the thread pool, not in the controller
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: ("busy",) in seen.messages)
        assert seen.restarted == [] and seen.quit == 0
    finally:
        window._busy.pop("a", None)
        _close(window, qapp)


def test_a_refused_close_cancels_the_restart_instead_of_quitting_anyway(
    tmp_path, qapp, monkeypatch
):
    window, _target, seen = _setup(tmp_path, monkeypatch)
    window._ask_restart = lambda version: True
    window._close_window_for_update = lambda: False  # the user pressed Cancel on a prompt
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: window._restart_target is not None)
        assert seen.restarted == [] and seen.quit == 0
    finally:
        _close(window, qapp)


def test_a_failed_update_says_why_and_goes_back_to_the_download_page(
    tmp_path, qapp, monkeypatch
):
    window, target, seen = _setup(tmp_path, monkeypatch, fail="the release checksums disagree")
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: any(m[0] == "failed" for m in seen.messages))
        assert _wait(qapp, lambda: not window._self_update.running)
        assert "disagree" in [m for m in seen.messages if m[0] == "failed"][0][1]
        assert target.read_bytes() == b"old" and not list(target.parent.glob(".*.download"))
        assert window.update_banner.action_button.text() == "Download"
        window.update_banner.action_button.click()
        assert seen.opened == [INFO.url]
    finally:
        _close(window, qapp)


def test_after_a_failure_the_button_opens_the_page_even_before_the_worker_reports_done(
    tmp_path, qapp, monkeypatch
):
    """`failed` is handled slightly before `finished`: a click in between must not be lost."""
    window, _target, seen = _setup(tmp_path, monkeypatch)
    try:
        window._show_update(INFO)
        window._on_update_failed("the release checksums disagree")
        window._self_update._running = True  # the worker has not signalled `finished` yet
        window.update_banner.action_button.click()
        assert seen.opened == [INFO.url]
    finally:
        window._self_update._running = False
        _close(window, qapp)


def test_pressing_update_twice_downloads_once(tmp_path, qapp, monkeypatch):
    window, _target, seen = _setup(tmp_path, monkeypatch)
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        window.update_banner.action_button.click()
        assert _wait(qapp, lambda: ("ask", "0.1.0b20") in seen.messages)
        assert seen.downloads == 1
    finally:
        _close(window, qapp)


def test_a_second_press_while_downloading_keeps_the_one_progress_dialog(
    tmp_path, qapp, monkeypatch
):
    import threading

    from PyQt6.QtWidgets import QProgressDialog

    window, _target, seen = _setup(tmp_path, monkeypatch)
    release = threading.Event()

    def slow(url, dest, sha256, **kwargs):
        release.wait(5)
        dest.write_bytes(b"new-bytes")

    monkeypatch.setattr(selfupdate, "download_verified", slow)
    try:
        window._show_update(INFO)
        window.update_banner.action_button.click()
        first = window._update_progress
        assert first is not None
        window.update_banner.action_button.click()
        assert window._update_progress is first
        assert len(window.findChildren(QProgressDialog)) == 1
        release.set()
        assert _wait(qapp, lambda: ("ask", "0.1.0b20") in seen.messages)
        assert window._update_progress is None
    finally:
        release.set()
        _close(window, qapp)


def test_the_old_backup_goes_once_the_new_version_runs(tmp_path, qapp, monkeypatch):
    running = tmp_path / "apps" / NAME
    running.parent.mkdir()
    running.write_bytes(b"new")
    backup = running.parent / "Nadare-0.1.0b19-x86_64.AppImage.previous"
    backup.write_bytes(b"old")
    monkeypatch.setattr(selfupdate, "appimage_target", lambda *a, **k: running)
    config = Config.load(tmp_path / "config.json")
    window = MainWindow(config)
    try:
        assert not backup.exists()
        assert running.exists()
    finally:
        _close(window, qapp)


def teardown_module(module):
    QThreadPool.globalInstance().waitForDone(2000)


def test_a_failure_reason_starts_with_a_capital_letter(qapp):
    """Seen on the VM: 'Nothing was changed.' was followed by 'the downloaded file ...'."""
    from nadare.gui.main_window import capitalised

    assert capitalised("the downloaded file does not match") == "The downloaded file does not match"
    assert capitalised("Already fine") == "Already fine"
    assert capitalised("") == ""
    assert capitalised("é cedilla") == "É cedilla"
