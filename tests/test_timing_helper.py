# SPDX-License-Identifier: GPL-3.0-or-later
"""The test time factor (#1024) is bounded and defaults to 1."""

from __future__ import annotations

import pytest
from _timing import scaled, time_factor


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 1.0), ("3", 3.0), ("0.2", 1.0), ("100", 20.0), ("x", 1.0)]
)
def test_the_factor(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("NADARE_TEST_TIME_FACTOR", raising=False)
    else:
        monkeypatch.setenv("NADARE_TEST_TIME_FACTOR", raw)
    assert time_factor() == expected
    assert scaled(2) == 2 * expected
