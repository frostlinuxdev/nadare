# SPDX-License-Identifier: GPL-3.0-or-later
"""Only a real umu signal_handler traceback is Stop noise; a lookalike is a real crash."""

from __future__ import annotations

from nadare.core.umunoise import StopNoiseFilter


def _run(lines):
    filt = StopNoiseFilter()
    out = []
    for line in lines:
        out += filt.feed(line)
    return out + filt.flush()


def test_a_traceback_that_only_mentions_the_umu_names_is_not_folded():
    other = [
        "Traceback (most recent call last):",
        '  File "/home/u/game/tools.py", line 12, in <module>',
        '    print("umu_run.py", "in signal_handler")',
        "ProcessLookupError: [Errno 3] No such process",
    ]
    assert _run(other) == other
