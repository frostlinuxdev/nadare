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
