# SPDX-License-Identifier: GPL-3.0-or-later
"""#624: following a growing log appends the new lines instead of re-rendering the whole tail.

Every tick that saw the file change re-read up to 512 KiB, re-split, re-filtered, re-masked and
``setPlainText``-ed all of it on the GUI thread, once a second during an install.
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.gui.logs_dialog import LogsDialog

H = "===== 2026-10-26T10:00:0{n} launch (G) | nadare 0.1.0b12 =====\n"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "G-launch.log"
    path.write_text(H.format(n=1) + "first\n", encoding="utf-8")
    return path


def _dialog(log):
    dlg = LogsDialog(["G"], profile="G", operation="launch", directory=log.parent, follow=False)
    renders: list[int] = []
    real = dlg._render
    dlg._render = lambda *a: (renders.append(1), real(*a))[1]
    return dlg, renders


def _append(path, text):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    stamp = os.stat(path).st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))  # a tick compares size and mtime


def test_new_lines_are_appended_without_a_full_render(qapp, log):
    dlg, renders = _dialog(log)
    _append(log, "second\nthird\n")
    dlg._tick()
    text = dlg.view.toPlainText()
    assert text.rstrip().endswith("first\nsecond\nthird")
    assert renders == []
    assert dlg.current_text().rstrip().endswith("third")


def test_appended_lines_are_filtered_and_redacted_like_the_rest(qapp, log):
    home = os.path.expanduser("~")
    dlg, _renders = _dialog(log)
    dlg.search.setText("boom")
    _append(log, f"ERROR boom at {home}/x\nquiet\n")
    dlg._tick()
    text = dlg.view.toPlainText()
    assert "ERROR boom" in text and "quiet" not in text and home not in text


def test_a_half_written_line_waits_for_its_end(qapp, log):
    dlg, _renders = _dialog(log)
    _append(log, "part")
    dlg._tick()
    assert "part" not in dlg.view.toPlainText()
    _append(log, "ial line\n")
    dlg._tick()
    assert "partial line" in dlg.view.toPlainText()


def test_a_new_session_with_latest_only_renders_just_that_session(qapp, log):
    dlg, _renders = _dialog(log)
    _append(log, H.format(n=2) + "new session\n")
    dlg._tick()
    text = dlg.view.toPlainText()
    assert "new session" in text and "first" not in text


def test_a_file_that_shrank_is_read_again(qapp, log):
    dlg, renders = _dialog(log)
    log.write_text("fresh\n", encoding="utf-8")
    stamp = os.stat(log).st_mtime_ns + 2_000_000
    os.utime(log, ns=(stamp, stamp))
    dlg._tick()
    assert dlg.view.toPlainText().strip() == "fresh"
    assert renders


def test_a_file_that_grows_between_read_and_stat_keeps_the_partial_line(qapp, log):
    from nadare.core import logview

    log.write_text("abc", encoding="utf-8")  # a half-written first line
    real = logview.read_tail_with_size
    appended = []
    seen = {"done": False}

    def grow_after_read(path, max_bytes=512 * 1024):
        result = real(path, max_bytes)
        if not seen["done"]:
            seen["done"] = True
            _append(log, "def\n")  # the file grows before _load_selected stats it
            appended.append(1)
        return result

    import pytest

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(logview, "read_tail_with_size", grow_after_read)
    try:
        dlg, _renders = _dialog(log)
    finally:
        monkeypatch.undo()
    assert appended
    _append(log, "ghi\n")
    dlg._tick()
    assert dlg.view.toPlainText().splitlines()[0] == "abcdef"
