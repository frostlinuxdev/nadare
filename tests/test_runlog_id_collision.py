# SPDX-License-Identifier: GPL-3.0-or-later
"""#972 item 3: two different profiles never share a log folder.

A name that needs cleaning ("Blue Archive") becomes ``blue-archive-fc51ab`` (slug + hash). A
profile literally named ``blue-archive-fc51ab`` used that folder too, so both games shared
rotation, listing and deletion. A safe name that looks like a generated id now gets its own hash
as well; every ordinary name keeps its folder (no migration).
"""

from __future__ import annotations

import pytest

from nadare.core import runlog


def test_a_name_that_looks_generated_does_not_take_another_games_folder():
    generated = runlog.game_id("Blue Archive")
    assert generated == "blue-archive-fc51ab"
    assert runlog.game_id(generated) != generated


@pytest.mark.parametrize(
    "name",
    ["pgr", "arknights-2", "chaos-zero-nightmare", "nikke", "game-12", "a.b_c-d", "wuwa-2026"],
)
def test_ordinary_names_keep_their_folder(name):
    assert runlog.game_id(name) == name


def test_ids_stay_stable_and_safe():
    for name in ("Blue Archive", "blue-archive-fc51ab", "x-abcdef", "Ünïcode Game"):
        first = runlog.game_id(name)
        assert first == runlog.game_id(name)
        assert runlog._SAFE_ID.fullmatch(first)


def test_distinct_names_get_distinct_folders():
    names = ["Blue Archive", "blue-archive-fc51ab", "blue archive", "BLUE ARCHIVE", "x-abcdef", "x"]
    ids = [runlog.game_id(n) for n in names]
    assert len(set(ids)) == len(ids)
