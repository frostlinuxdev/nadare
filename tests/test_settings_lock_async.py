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


# ---- relink: "I moved it myself" -------------------------------------------
def _moved_prefix(tmp_path):
    new = tmp_path / "elsewhere" / "pfx"
    (new / "drive_c").mkdir(parents=True)
    return new


def _relink_setup(tmp_path, qapp, monkeypatch, state="installed"):
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    _fake_backend(monkeypatch, SimpleNamespace(state=state))
    new = _moved_prefix(tmp_path)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(new))
    answers = []
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.append(a[2]) or yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: answers.append(a[2]))
    page = SettingsPage()
    config = _config(tmp_path)
    page.load(config, "sample")
    _settle(qapp)
    return page, config, new, answers


def test_relink_points_a_locked_profile_at_the_moved_prefix(tmp_path, qapp, monkeypatch):
    page, config, new, _ = _relink_setup(tmp_path, qapp, monkeypatch)
    try:
        assert page.option_widget("prefix").isReadOnly()
        assert not page.relink_button.isHidden()
        page.relink_button.click()
        _settle(qapp)  # the game's state is re-checked off the GUI thread first
        assert config.profiles["sample"]["prefix"] == str(new)
        assert Config.load(tmp_path / "config.json").profiles["sample"]["prefix"] == str(new)
        assert page.option_widget("prefix").text() == str(new)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_relink_is_refused_while_the_game_runs(tmp_path, qapp, monkeypatch):
    page, config, new, _ = _relink_setup(tmp_path, qapp, monkeypatch, state="running")
    try:
        assert not page.relink_button.isEnabled()
        page.relink_button.click()
        assert config.profiles["sample"]["prefix"] == str(tmp_path / "pfx")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_relink_with_a_folder_that_is_no_prefix_changes_nothing(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    page, config, new, answers = _relink_setup(tmp_path, qapp, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(empty))
    try:
        page.relink_button.click()
        assert config.profiles["sample"]["prefix"] == str(tmp_path / "pfx")
        assert answers and "drive_c" in answers[-1]
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_relink_button_survives_loading_another_profile(tmp_path, qapp, monkeypatch):
    page, config, new, _ = _relink_setup(tmp_path, qapp, monkeypatch)
    try:
        page.load(config, "sample")
        _settle(qapp)
        assert not page.relink_button.isHidden()
        page.relink_button.click()  # would raise if the button had been deleted
        _settle(qapp)
        assert config.profiles["sample"]["prefix"] == str(new)
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- move: Nadare moves the prefix itself ----------------------------------
def _move_setup(tmp_path, qapp, monkeypatch, state="installed"):
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    _fake_backend(monkeypatch, SimpleNamespace(state=state))
    src = tmp_path / "pfx"
    (src / "drive_c" / "Game").mkdir(parents=True)
    (src / "drive_c" / "Game" / "a.bin").write_bytes(b"x" * 100)
    target_parent = tmp_path / "bigdisk"
    target_parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(target_parent))
    texts = []
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: texts.append(a[2]) or yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: texts.append(a[2]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: texts.append(a[2]))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: texts.append(a[2]))
    page = SettingsPage()
    config = _config(tmp_path, install_dir=str(src / "drive_c" / "Game"))
    page.load(config, "sample")
    _settle(qapp)
    return page, config, src, target_parent, texts


def test_move_moves_the_prefix_and_updates_the_profile(tmp_path, qapp, monkeypatch):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        assert not page.move_button.isHidden()
        page.move_button.click()
        _settle(qapp)
        new = parent / "pfx"
        assert (new / "drive_c" / "Game" / "a.bin").is_file() and not src.exists()
        saved = Config.load(tmp_path / "config.json").profiles["sample"]
        assert saved["prefix"] == str(new)
        assert saved["install_dir"] == str(new / "drive_c" / "Game")
        assert page.option_widget("prefix").text() == str(new)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_cross_disk_move_keeps_the_old_copy_and_says_so(tmp_path, qapp, monkeypatch):
    from nadare.core import relocate

    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert src.is_dir() and (parent / "pfx" / "drive_c").is_dir()
        assert any(str(src) in t and "delete" in t.lower() for t in texts)
        assert config.profiles["sample"]["prefix"] == str(parent / "pfx")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_failed_move_changes_nothing(tmp_path, qapp, monkeypatch):
    from nadare.core import relocate

    def boom(*a, **k):
        raise relocate.RelocateError("Not enough space at /x")

    monkeypatch.setattr(relocate, "move_tree", boom)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert config.profiles["sample"]["prefix"] == str(src)
        assert any("Not enough space" in t for t in texts), texts
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_move_is_disabled_while_the_game_runs(tmp_path, qapp, monkeypatch):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch, state="running")
    try:
        assert not page.move_button.isEnabled()
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- review fixes: dead buttons, unknown state ------------------------------
def test_reloading_a_profile_without_a_prefix_option_never_touches_dead_buttons(
    tmp_path, qapp, monkeypatch
):
    _fake_backend(monkeypatch, SimpleNamespace(state="installed"))
    config = _config(tmp_path)
    config.add_profile("steamy", backend="steam", title="S")
    page = SettingsPage()
    try:
        from PyQt6.QtCore import QEvent

        for name in ("sample", "steamy", "sample", "steamy", "steamy"):
            page.load(config, name)
            _settle(qapp)
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # what a real loop does
        assert page.relink_button.isHidden()  # no prefix option: nothing to relink
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_buttons_stay_disabled_until_the_game_is_known_to_be_idle(
    tmp_path, qapp, monkeypatch
):
    def boom():
        raise OSError("probe failed")

    _fake_backend(monkeypatch, boom)
    page = SettingsPage()
    try:
        page.load(_config(tmp_path), "sample")
        _settle(qapp)
        assert not page.relink_button.isEnabled()  # state unknown: could be running
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_running_flag_does_not_leak_into_the_next_profile(tmp_path, qapp, monkeypatch):
    states = iter(["running", "installed"])
    _fake_backend(monkeypatch, lambda: SimpleNamespace(state=next(states)))
    config = _config(tmp_path)
    page = SettingsPage()
    try:
        page.load(config, "sample")
        _settle(qapp)
        assert not page.relink_button.isEnabled()
        page.load(config, "sample")
        assert not page.relink_button.isEnabled()  # until the new probe answers
        _settle(qapp)
        assert page.relink_button.isEnabled()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_move_finishes_into_the_profile_it_started_for(tmp_path, qapp, monkeypatch):
    import threading

    from nadare.core import relocate

    release = threading.Event()
    real = relocate.move_tree
    monkeypatch.setattr(
        relocate, "move_tree", lambda *a, **k: (release.wait(5), real(*a, **k))[1]
    )
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    other_pfx = tmp_path / "other"
    other_pfx.mkdir()
    config.add_profile(
        "other", backend="stove-czn", prefix=str(other_pfx),
        owned_trees=["prefix"], owned_paths={"prefix": str(other_pfx)},
    )
    try:
        page.move_button.click()
        page.load(config, "other")  # the user opens another game while it moves
        release.set()
        _settle(qapp)
        assert config.profiles["sample"]["prefix"] == str(parent / "pfx")
        assert config.profiles["other"]["prefix"] == str(other_pfx)
        assert config.profiles["other"]["owned_paths"] == {"prefix": str(other_pfx)}
    finally:
        release.set()
        page.deleteLater()
        qapp.processEvents()


def test_a_move_is_refused_while_another_operation_holds_the_profile(
    tmp_path, qapp, monkeypatch
):
    from nadare.core import process

    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    lock = process.OperationLock("sample")
    lock.acquire()
    try:
        page.move_button.click()
        _settle(qapp)
        assert src.is_dir() and config.profiles["sample"]["prefix"] == str(src)
        assert any("Another Nadare" in t for t in texts)
    finally:
        lock.release()
        page.deleteLater()
        qapp.processEvents()


# ---- b17 edge-case audit: move/relink error paths ---------------------------
def test_a_move_finishing_after_the_page_is_destroyed_does_not_crash(
    tmp_path, qapp, monkeypatch
):
    import threading

    from PyQt6 import sip

    from nadare.core import relocate

    release = threading.Event()
    real = relocate.move_tree
    monkeypatch.setattr(
        relocate, "move_tree", lambda *a, **k: (release.wait(5), real(*a, **k))[1]
    )
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    page.move_button.click()
    sip.delete(page)  # the window is closed while the copy runs
    release.set()
    _settle(qapp)  # pytest-qt fails the test if a slot raised
    assert config.profiles["sample"]["prefix"] == str(parent / "pfx")  # still recorded


def test_a_failed_config_save_after_the_move_says_where_the_folder_is_now(
    tmp_path, qapp, monkeypatch
):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    config.save = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left"))
    try:
        page.move_button.click()
        _settle(qapp)
        assert any(str(parent / "pfx") in t and "I moved it myself" in t for t in texts), texts
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_folder_that_is_no_prefix_is_not_moved(tmp_path, qapp, monkeypatch):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    import shutil

    shutil.rmtree(src / "drive_c")  # not a prefix any more
    try:
        page.move_button.click()
        _settle(qapp)
        assert src.is_dir() and not (parent / "pfx").exists()
        assert any("drive_c" in t for t in texts)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_profile_with_no_prefix_is_not_moved(tmp_path, qapp, monkeypatch):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    config.profiles["sample"]["prefix"] = ""
    page.option_widget("prefix").setText("")
    try:
        page.move_button.click()
        _settle(qapp)
        assert any("no prefix" in t.lower() for t in texts)
        assert not (parent / "pfx").exists()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_game_started_after_the_page_loaded_blocks_the_move(tmp_path, qapp, monkeypatch):
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    _fake_backend(monkeypatch, SimpleNamespace(state="running"))  # started from a desktop icon
    try:
        page.move_button.click()
        _settle(qapp)
        assert src.is_dir() and not (parent / "pfx").exists()
        assert any("running" in t.lower() for t in texts), texts
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_relink_is_refused_while_another_operation_holds_the_profile(
    tmp_path, qapp, monkeypatch
):
    from nadare.core import process

    page, config, new, answers = _relink_setup(tmp_path, qapp, monkeypatch)
    lock = process.OperationLock("sample")
    lock.acquire()
    try:
        page.relink_button.click()
        _settle(qapp)
        assert config.profiles["sample"]["prefix"] == str(tmp_path / "pfx")
        assert any("Another Nadare" in t for t in answers), answers
    finally:
        lock.release()
        page.deleteLater()
        qapp.processEvents()


def test_a_second_move_request_while_one_runs_starts_nothing(tmp_path, qapp, monkeypatch):
    import threading

    from nadare.core import relocate

    release = threading.Event()
    calls = []
    real = relocate.move_tree

    def gated(*a, **k):
        calls.append(1)
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(relocate, "move_tree", gated)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page._run_move(config, "sample", src, parent / "pfx")
        page._run_move(config, "sample", src, parent / "pfx")
        release.set()
        _settle(qapp)
        assert calls == [1]
        assert not any("Another Nadare" in t for t in texts)
    finally:
        release.set()
        page.deleteLater()
        qapp.processEvents()
