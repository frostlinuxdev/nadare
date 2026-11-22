# SPDX-License-Identifier: GPL-3.0-or-later
"""#1352: every Move says it moved, and leaves a log.

Moving Chaos Zero Nightmare into a library on the same drive was an instant rename: no visible
feedback and no log, so the maintainer could not tell whether anything had happened.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere

from nadare.core import movelog, paths, runlog
from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage
from nadare.gui.settings_parts.move import MoveMixin


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


def _wait(qapp, condition, seconds=10.0):
    import time

    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def _logs(name="sample"):
    folder = paths.logs_dir() / runlog.game_id(name)
    return sorted(folder.glob("move-*.log")) if folder.is_dir() else []


# ---- the log itself --------------------------------------------------------
def test_a_move_log_names_from_to_how_and_the_outcome(tmp_path):
    path = movelog.write(
        "sample",
        title="Chaos Zero Nightmare",
        src=tmp_path / "a",
        dst=tmp_path / "b",
        how="renamed (same drive)",
        outcome="moved",
        logs=tmp_path / "logs",
        now=datetime(2026, 11, 15, 11, 48, 0),
    )
    assert path is not None and path.name == "move-20261115-114800.log"
    text = path.read_text()
    assert "move (Chaos Zero Nightmare)" in text
    assert f"from: {tmp_path / 'a'}" in text and f"to: {tmp_path / 'b'}" in text
    assert "renamed (same drive)" in text
    assert "[end] moved" in text
    assert runlog.operation_of(path) == "move"  # the log viewer lists it with the others


def test_a_line_can_be_added_later_and_nothing_ever_raises(tmp_path):
    path = movelog.write(
        "sample", title="A", src=tmp_path, dst=tmp_path, how="", outcome="moved",
        logs=tmp_path / "logs",
    )
    movelog.append(path, "original removed")
    assert path.read_text().rstrip().endswith("original removed")
    movelog.append(None, "x")  # no log: nothing happens
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert movelog.write(
        "sample", title="A", src=tmp_path, dst=tmp_path, how="", outcome="moved", logs=blocked
    ) is None


# ---- the Settings page -----------------------------------------------------
def _setup(tmp_path, qapp, monkeypatch, state="installed"):
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    def create_backend(profile, config):
        answer = state() if callable(state) else state
        return SimpleNamespace(status=lambda: SimpleNamespace(state=answer))

    patch_everywhere(monkeypatch, settings_mod, "create_backend", create_backend)
    src = tmp_path / "pfx"
    (src / "drive_c" / "Game").mkdir(parents=True)
    (src / "drive_c" / "Game" / "a.bin").write_bytes(b"x" * 100)
    parent = tmp_path / "bigdisk"
    parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(parent))
    texts = []
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: texts.append(a[2]) or yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: texts.append(a[2]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: texts.append(a[2]))
    confirmed = []
    monkeypatch.setattr(MoveMixin, "_confirm_moved", lambda self, text: confirmed.append(text))
    page = SettingsPage()
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "sample", backend="stove-czn", prefix=str(src), title="Chaos Zero Nightmare",
        install_dir=str(src / "drive_c" / "Game"),
    )
    page.load(config, "sample")
    _settle(qapp)
    return page, src, parent, texts, confirmed


def test_an_instant_move_says_so_and_writes_a_log(tmp_path, qapp, monkeypatch):
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert not src.exists()
        assert confirmed and "Chaos Zero Nightmare" in confirmed[0]
        assert str(parent / "pfx") in confirmed[0]
        logs = _logs()
        assert len(logs) == 1
        text = logs[0].read_text()
        assert "renamed (same drive)" in text and "[end] moved" in text
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_copy_to_another_disk_logs_the_copy_and_the_original(tmp_path, qapp, monkeypatch):
    from nadare.core import relocate, removal

    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    monkeypatch.setattr(removal, "prefix_made_by_nadare", lambda *a, **kw: True)
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert _wait(qapp, lambda: not src.exists())
        assert confirmed
        assert _wait(qapp, lambda: "original removed" in (_logs() or [None])[0].read_text())
        assert "copied and checked" in _logs()[0].read_text()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_refused_move_is_logged_too(tmp_path, qapp, monkeypatch):
    """The game starts between the question and the move: the worker's re-check refuses."""
    answers = {"state": "installed"}
    page, src, parent, texts, confirmed = _setup(
        tmp_path, qapp, monkeypatch, state=lambda: answers["state"]
    )
    try:
        answers["state"] = "running"
        page.move_button.click()
        _settle(qapp)
        assert src.exists() and not confirmed  # nothing moved, nothing confirmed
        text = _logs()[0].read_text()
        assert "[end] not moved" in text and "running" in text
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_confirmation_is_one_plain_box(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    seen = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text: seen.append((title, text))
    )
    MoveMixin._real_confirm_moved(None, "Moved A to /x.")
    assert seen == [("Moved", "Moved A to /x.")]


# ---- #1754: the "Moved" box must be the only thing up, and the game free --------------------
def _visible_progress_dialogs():
    from PyQt6.QtWidgets import QProgressDialog

    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QProgressDialog)
            and not w.isHidden()]


def _watch_the_box(qapp, monkeypatch):
    """Replace the "Moved" box by a probe that runs the event loop like a modal box does."""
    from nadare.core import process

    seen = []

    def probe(self, text):
        for _ in range(20):  # a modal box runs a nested loop: queued signals arrive here
            qapp.processEvents()
        seen.append({
            "progress_dialogs": len(_visible_progress_dialogs()),
            "busy": process.operation_in_progress("sample"),
        })

    monkeypatch.setattr(MoveMixin, "_confirm_moved", probe)
    return seen


@pytest.mark.parametrize("other_disk", [False, True])
def test_no_progress_dialog_comes_back_over_the_moved_box(
    tmp_path, qapp, monkeypatch, other_disk
):
    """#1754: the closed "Moving" dialog came back (minimum duration 0, a late progress
    signal) as a modal window over the "Moved" box, which then took no input."""
    if other_disk:
        from nadare.core import relocate, removal

        monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
        monkeypatch.setattr(removal, "prefix_made_by_nadare", lambda *a, **kw: True)
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    seen = _watch_the_box(qapp, monkeypatch)
    try:
        page.move_button.click()
        assert _wait(qapp, lambda: bool(seen))
        assert seen[0]["progress_dialogs"] == 0
        _settle(qapp)
        assert _visible_progress_dialogs() == []
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_the_game_is_free_while_the_moved_box_is_up(tmp_path, qapp, monkeypatch):
    """#1754: the lock was released only after the box closed, so a box that could not be
    closed left the game "busy" (no Launch) until Nadare was killed."""
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    seen = _watch_the_box(qapp, monkeypatch)
    try:
        page.move_button.click()
        assert _wait(qapp, lambda: bool(seen))
        assert seen[0]["busy"] is False
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_cancel_on_the_progress_dialog_still_closes_it(tmp_path, qapp, monkeypatch):
    """#1754: finish() disconnected every slot of `canceled`, QProgressDialog's own too."""
    import inspect

    from nadare.gui.settings_parts import move

    source = inspect.getsource(move.MoveMixin._run_move)
    assert "dialog.canceled.disconnect()" not in source


def test_a_late_progress_after_the_move_touches_nothing(tmp_path, qapp, monkeypatch):
    """#1754: progress queued before the end of a copy arrives after done(); it must not reach
    the progress dialog (gone by then; before, the closed dialog could show itself again)."""
    from nadare.core import relocate, removal

    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    monkeypatch.setattr(removal, "prefix_made_by_nadare", lambda *a, **kw: True)
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        assert _wait(qapp, lambda: bool(confirmed))
        _settle(qapp)
        assert _visible_progress_dialogs() == []
        from PyQt6.QtWidgets import QProgressDialog

        assert page.findChildren(QProgressDialog) == []
    finally:
        page.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("other_disk", [False, True])
def test_the_end_of_a_move_never_runs_inside_the_progress_dialog(
    tmp_path, qapp, monkeypatch, other_disk
):
    """#1754 root cause: a modal QProgressDialog runs the event loop in setValue(), so done()
    ran inside it and opened the "Moved" box from within the dialog's own setValue()."""
    from PyQt6.QtWidgets import QProgressDialog

    if other_disk:
        from nadare.core import relocate, removal

        monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
        monkeypatch.setattr(removal, "prefix_made_by_nadare", lambda *a, **kw: True)
    depth = []
    real_set_value = QProgressDialog.setValue

    def set_value(self, value):
        depth.append(1)
        try:
            for _ in range(5):  # what a modal setValue() does, made certain
                qapp.processEvents()
            real_set_value(self, value)
        finally:
            depth.pop()

    monkeypatch.setattr(QProgressDialog, "setValue", set_value)
    page, src, parent, texts, confirmed = _setup(tmp_path, qapp, monkeypatch)
    inside = []
    monkeypatch.setattr(MoveMixin, "_confirm_moved", lambda self, text: inside.append(len(depth)))
    try:
        page.move_button.click()
        assert _wait(qapp, lambda: bool(inside))
        assert inside == [0]
    finally:
        page.deleteLater()
        qapp.processEvents()
