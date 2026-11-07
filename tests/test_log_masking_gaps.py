# SPDX-License-Identifier: GPL-3.0-or-later
"""Secrets in exception message continuations and in free-form settings (#968)."""

from __future__ import annotations

from nadare.core import events, report


def test_an_indented_exception_message_continuation_is_masked() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 3, in f\n'
        "    raise ValueError(msg)\n"
        "ValueError: request failed\n"
        "    password=SYNTHETIC-SECRET-VALUE\n"
        "    Authorization: Bearer SYNTHETICTOKEN123"
    )
    out = events.mask_traceback(text)
    assert "SYNTHETIC-SECRET-VALUE" not in out
    assert "SYNTHETICTOKEN123" not in out
    assert '  File "x.py", line 3, in f' in out  # frames stay readable
    assert "    raise ValueError(msg)" in out


def test_launch_args_secrets_are_masked_but_paths_stay_short() -> None:
    assert "SYNTHETIC" not in str(report._safe_setting("launch_args", "-x --password=SYNTHETIC1"))
    assert report._safe_setting("launch_args", "/home/u/games/run.sh") == "run.sh"
    assert report._safe_setting("launch_args", "-windowed") == "-windowed"
