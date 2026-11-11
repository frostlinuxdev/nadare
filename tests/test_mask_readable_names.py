# SPDX-License-Identifier: GPL-3.0-or-later
"""#242: the 12-23 character heuristic masked runner tags and game names in logs.

A run made of readable words and short numbers (``GE-Proton9-20``, ``WutheringWaves``) is a
name, not a secret; a run that looks generated is still masked, and 24+ characters always are.
"""

from __future__ import annotations

import pytest

from nadare.core.events import mask


@pytest.mark.parametrize(
    "name",
    [
        "GE-Proton9-20",
        "GE-Proton10-25",
        "Proton-Experimental",
        "proton-cachyos-10",
        "wine-ge-8-26",
        "WutheringWaves",
        "StarRail_Data",
        "UnityPlayer_x64",
        "Client-Win64-Shipping",
        "NIKKE_Launcher",
        "ZenlessZoneZero",
        "BlueArchive",
    ],
)
def test_readable_names_are_left_alone(name):
    text = f"using {name} for this game"
    assert mask(text) == text


@pytest.mark.parametrize(
    "secret",
    [
        "Abcdef123456",  # the L1 example: a word glued to a long number
        "a3f9c2d1e4b5",  # hex
        "AbCdEfGhIjKlMn",  # mixed case with no word shape
        "x7Kp2QmZ9wLr",
        "TOKEN-12345678",
        "eyJhbGciOiJIUzI1",  # a JWT head
        "Zm9vYmFyYmF6cXV4",  # base64
        "ABCD-EFGH-IJKL",
        "TOKEN-1234-TOKEN",
        "AB12-CD34-EF56",
    ],
)
def test_generated_looking_runs_are_still_masked(secret):
    assert secret not in mask(f"value {secret} end")


def test_long_runs_are_always_masked_even_when_readable():
    text = "GE-Proton-Experimental-Bleeding-Edge"  # 36 chars: the 24+ rule is unchanged
    assert text not in mask(text)
