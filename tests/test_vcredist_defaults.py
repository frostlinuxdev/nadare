# SPDX-License-Identifier: GPL-3.0-or-later
"""The VC++ step is per game: skipped where the research showed it is not needed."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.core import backends

SKIPPED = (
    "launcher-nikke",
    "launcher-nte",
    "direct-nte",
    "direct-endfield",
    "direct-genshin",
    "direct-hsr",
    "direct-zzz",
)
KEPT = (
    "arknights-yostar",
    "stellasora-yostar",
    "direct-wuwa",
    "direct-pgr",
    "direct-hi3",
    "direct-gfl2",
    "direct-bd2",
    "direct-mongil",
    "direct-aethergazer",
)


def _run(tmp_path, monkeypatch, kind, **profile):
    calls = []
    monkeypatch.setattr(ak.vcredist, "ensure", lambda *a, **k: calls.append(1))
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    backend = backends.get(kind)(make_context(tmp_path, kind, values))
    backend._run_vcredist(created=False)
    return calls


@pytest.mark.parametrize("kind", SKIPPED)
def test_games_that_do_not_need_it_skip_the_step(tmp_path, monkeypatch, kind):
    assert _run(tmp_path, monkeypatch, kind) == []


@pytest.mark.parametrize("kind", SKIPPED)
def test_a_user_can_turn_it_on_for_a_stuttering_game(tmp_path, monkeypatch, kind):
    assert _run(tmp_path, monkeypatch, kind, vcredist=True) == [1]


@pytest.mark.parametrize("kind", KEPT)
def test_unproven_games_keep_the_step(tmp_path, monkeypatch, kind):
    assert _run(tmp_path, monkeypatch, kind) == [1]
