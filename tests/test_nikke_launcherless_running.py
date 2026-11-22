# SPDX-License-Identifier: GPL-3.0-or-later
"""A launcherless NIKKE Play reports the game as running, like every other Wine game.

The sidebar and the library learn "Playing" from the launch's ``game_running`` event. The
launcher path gets it from the process watch; the Sail path used to start the game without
that watch, so NIKKE played while the sidebar still showed it idle (maintainer, 2026-11-20).
"""

from __future__ import annotations

import dataclasses
import time

from test_nikke_game_auth_cache_1420 import Runner, play  # noqa: F401 - play is a fixture
from test_nikke_launcherless_launch import make_install

from nadare.backends.winegame_parts import launching


class SlowRunner(Runner):
    """The Sail host that keeps the game up long enough for one watch poll."""

    def __call__(self, argv, env, on_line=None, cancel=None, **kwargs):
        code = super().__call__(argv, env, on_line=on_line, cancel=cancel, **kwargs)
        time.sleep(1.6)
        return code


def _events(backend):
    seen = []
    backend.ctx = dataclasses.replace(backend.ctx, emit=seen.append)
    return seen


def test_a_launcherless_play_says_the_game_is_running(tmp_path, play, monkeypatch):  # noqa: F811
    backend = make_install(tmp_path)
    queries = []

    def scan(query, *args, **kwargs):
        queries.append(query)
        return [4242] if "nikke.exe" in getattr(query, "names", ()) else []

    monkeypatch.setattr(launching, "scan_pids", scan)
    seen = _events(backend)

    assert play(backend, SlowRunner(backend)) == 0

    assert [e.kind for e in seen].count("game_running") == 1
    # The game itself, not the Sail host or the launcher, which start first.
    watched = [q for q in queries if "nikke.exe" in getattr(q, "names", ())]
    assert watched and all(q.names == ("nikke.exe",) and q.require_name for q in watched)


def test_only_the_sail_host_is_not_yet_playing(tmp_path, play, monkeypatch):  # noqa: F811
    backend = make_install(tmp_path)
    monkeypatch.setattr(launching, "scan_pids", lambda query, *a, **k: [])
    seen = _events(backend)

    assert play(backend, SlowRunner(backend)) == 0

    assert "game_running" not in [e.kind for e in seen]
