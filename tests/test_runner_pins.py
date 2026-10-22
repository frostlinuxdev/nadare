# SPDX-License-Identifier: GPL-3.0-or-later
"""DW-Proton 11.0-15 is a tested pin in both runner tables, and they agree."""

from __future__ import annotations

from nadare.backends.czn import runners as czn_runners
from nadare.core import runners as core_runners


def test_dwproton_has_a_tested_pin():
    tag, sha = core_runners.RUNNERS["dwproton"].tested
    assert tag == "dwproton-11.0-15"
    assert sha == "8066dec3d7f532d55e5b6d697a61d6d4a3091f859a1ef9b2943895aa1b947afd"
    assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")


def test_core_and_czn_pins_match_for_every_runner():
    for rid, spec in core_runners.RUNNERS.items():
        assert czn_runners.RUNNERS[rid]["tested"] == spec.tested, rid
