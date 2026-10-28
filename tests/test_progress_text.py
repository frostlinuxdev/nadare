# SPDX-License-Identifier: GPL-3.0-or-later
"""Download progress is worded in sizes people can read, not raw byte counts."""

from __future__ import annotations

from nadare.core import download
from nadare.core.events import Event


def _message(done: int, total: int) -> str:
    seen: list[Event] = []
    download._emit_progress(seen.append, "install", done, total)
    return seen[0].message


def test_progress_is_in_readable_units():
    assert _message(291504128, 66131233710) == "install: 278 MiB / 62 GiB"


def test_progress_with_an_unknown_total_shows_only_what_is_done():
    assert _message(5 * 1024 * 1024, 0) == "install: 5 MiB"


def test_progress_event_keeps_the_raw_numbers_for_callers():
    seen: list[Event] = []
    download._emit_progress(seen.append, "download", 50, 200)
    assert seen[0].data == {"stage": "download", "done": 50, "total": 200}
    assert seen[0].progress == 0.25
