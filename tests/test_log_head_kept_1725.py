# SPDX-License-Identifier: GPL-3.0-or-later
"""A cut log keeps its start (the ``$`` command line) as well as its end (#1725)."""

from __future__ import annotations

import zipfile

from nadare.core import logview, report

HEAD = (
    "===== 2026-11-20 20:02 launch (Game) | nadare 0.1.0b28 =====\n"
    "$ /usr/bin/umu-run /games/Game/Game.exe\n"
)
CRASH = (
    "wine: Unhandled page fault on read access to 000000000000036E at address "
    "00006FFFFFC174D7 (thread {n:05x}), starting debugger...\n"
)


def _crash_loop(path, lines):
    path.write_text(HEAD + "".join(CRASH.format(n=i) for i in range(lines)) + "last line\n")


def test_read_head_and_tail_keeps_both_ends_and_says_what_was_cut(tmp_path):
    log = tmp_path / "launch.log"
    _crash_loop(log, 2000)
    size = log.stat().st_size
    text = logview.read_head_and_tail(log, head_bytes=1024, tail_bytes=4096)
    assert text.startswith(HEAD)
    assert text.endswith("last line\n")
    assert "bytes cut" in text
    assert len(text.encode()) < 1024 + 4096 + 200
    assert f"{size - len(text.encode())}" in text or "bytes cut" in text


def test_read_head_and_tail_returns_a_small_log_whole(tmp_path):
    log = tmp_path / "launch.log"
    _crash_loop(log, 3)
    assert logview.read_head_and_tail(log, head_bytes=1024, tail_bytes=4096) == log.read_text()


def test_read_head_and_tail_keeps_whole_lines_only(tmp_path):
    log = tmp_path / "launch.log"
    _crash_loop(log, 2000)
    text = logview.read_head_and_tail(log, head_bytes=100, tail_bytes=500)
    for line in text.splitlines():
        assert line.startswith(("=====", "$ ", "wine: Unhandled", "last line", "…"))


def test_the_bundle_keeps_the_command_line_of_a_long_crash_loop(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _crash_loop(logs / "G-launch.log", 40_000)  # about 4.5 MB
    out = tmp_path / "b.zip"
    logview.bundle(["G"], logs, out, redact=False)
    with zipfile.ZipFile(out) as zf:
        text = zf.read("G-launch.log").decode()
        readme = zf.read("README.txt").decode()
    assert "$ /usr/bin/umu-run /games/Game/Game.exe" in text
    assert text.rstrip().endswith("last line")
    assert len(text.encode()) <= logview.BUNDLE_FILE_BYTES + 200
    assert "first" in readme and "last" in readme


def test_the_report_tail_keeps_the_command_line_of_a_long_log(tmp_path):
    log = tmp_path / "launch-1.log"
    _crash_loop(log, 5000)
    text = report._tail(log)
    assert "$ /usr/bin/umu-run /games/Game/Game.exe" in text
    assert text.splitlines()[-1] == "last line"
    assert "cut" in text


def test_the_report_tail_of_a_short_log_has_no_cut_line(tmp_path):
    log = tmp_path / "launch-1.log"
    _crash_loop(log, 10)
    assert "cut" not in report._tail(log)
