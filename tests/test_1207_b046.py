# SPDX-License-Identifier: GPL-3.0-or-later
"""Registry helpers must treat a non-string kind as unknown, not crash."""

from __future__ import annotations

from nadare.core import backends


def test_one_per_game_is_safe_for_a_non_string_kind():
    assert backends.one_per_game(["direct-pgr"]) is False
