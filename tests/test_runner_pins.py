# SPDX-License-Identifier: GPL-3.0-or-later
"""DW-Proton 11.0-15 is the tested pin in the runner table."""

from __future__ import annotations

from nadare.core import runners as core_runners


def test_dwproton_has_a_tested_pin():
    tag, sha = core_runners.RUNNERS["dwproton"].tested
    assert tag == "dwproton-11.0-15"
    assert sha == "8066dec3d7f532d55e5b6d697a61d6d4a3091f859a1ef9b2943895aa1b947afd"
    assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")

