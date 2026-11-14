# SPDX-License-Identifier: GPL-3.0-or-later
"""umu-run's Stop traceback does not land in the launch log (#675).

When a game is stopped, umu-run's SIGTERM handler kills its process tree with ``os.kill``; a
child that already exited raises ProcessLookupError, which umu logs with ``log.exception``.
That upstream noise went into the user's log and into "Report a problem". It becomes one plain
line; every other traceback passes through untouched.
"""

from __future__ import annotations

from nadare.core.umunoise import NOTE, StopNoiseFilter

UMU_BLOCK = [
    "ERROR: [Errno 3] No such process",
    "Traceback (most recent call last):",
    '  File "/usr/lib/python3/dist-packages/umu/__main__.py", line 98, in main',
    "    return umu_run(args)",
    '  File "/usr/lib/python3/dist-packages/umu/umu_run.py", line 889, in umu_run',
    "    return run_command(command)",
    '  File "/usr/lib/python3/dist-packages/umu/umu_run.py", line 696, in signal_handler',
    "    os.kill(p, sig)",
    "ProcessLookupError: [Errno 3] No such process",
]


def _run(lines):
    f = StopNoiseFilter()
    out = []
    for line in lines:
        out += f.feed(line)
    return out + f.flush()


def test_the_umu_stop_traceback_becomes_one_line():
    assert _run(["game says hi", *UMU_BLOCK, "after"]) == ["game says hi", NOTE, "after"]


def test_the_block_without_the_leading_error_line_is_also_folded():
    assert _run(UMU_BLOCK[1:]) == [NOTE]


def test_another_traceback_passes_through_unchanged():
    other = [
        "Traceback (most recent call last):",
        '  File "/app/nadare/core/thing.py", line 3, in f',
        "    x = 1 / 0",
        "ZeroDivisionError: division by zero",
    ]
    assert _run(["a", *other, "b"]) == ["a", *other, "b"]


def test_a_process_lookup_error_outside_umu_is_kept():
    other = [
        "Traceback (most recent call last):",
        '  File "/home/u/tool.py", line 9, in stop',
        "    os.kill(p, sig)",
        "ProcessLookupError: [Errno 3] No such process",
    ]
    assert _run(other) == other


def test_an_errno_line_not_followed_by_a_traceback_is_kept():
    assert _run(["ERROR: [Errno 3] No such process", "wine: done"]) == [
        "ERROR: [Errno 3] No such process",
        "wine: done",
    ]


def test_a_cut_off_traceback_at_the_end_is_not_lost():
    assert _run(UMU_BLOCK[:5]) == UMU_BLOCK[:5]


def test_a_runaway_block_is_flushed_not_held_forever():
    lines = ["Traceback (most recent call last):"] + [f"    frame {i}" for i in range(200)]
    f = StopNoiseFilter()
    held_out = []
    for line in lines:
        held_out += f.feed(line)
    assert len(held_out) >= len(lines) - 100  # never holds more than a bounded tail
    assert held_out + f.flush() == lines


# ---- wired into the game launch -------------------------------------------------------------
from _backend_helpers import make_context  # noqa: E402

from nadare.backends import direct  # noqa: E402


def test_the_launch_log_gets_the_note_not_the_traceback(tmp_path, monkeypatch):
    backend = direct.StarRailBackend(make_context(tmp_path, "direct-hsr", {}))
    logged = []
    monkeypatch.setattr(backend, "info", lambda m, **k: logged.append(m))
    backend._begin_game_output()
    for line in ["game says hi", *UMU_BLOCK]:
        backend._on_game_line(line)
    backend._end_game_output()
    assert logged == ["game says hi", NOTE]
