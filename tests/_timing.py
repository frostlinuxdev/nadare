# SPDX-License-Identifier: GPL-3.0-or-later
"""Wall-clock bounds in tests, scaled for a loaded CI runner (#1024).

A few tests check that something finishes "in time". On the shared runner with ``-n auto`` they
failed now and then under load, not because the code was slow. ``NADARE_TEST_TIME_FACTOR``
(default 1, CI sets more) stretches those bounds without hiding a real hang.
"""

from __future__ import annotations

import os


def time_factor() -> float:
    try:
        value = float(os.environ.get("NADARE_TEST_TIME_FACTOR", "1"))
    except ValueError:
        return 1.0
    return min(max(value, 1.0), 20.0)


def scaled(seconds: float) -> float:
    """``seconds`` stretched by :func:`time_factor`."""
    return seconds * time_factor()
