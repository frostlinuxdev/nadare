# SPDX-License-Identifier: GPL-3.0-or-later
"""The ``wineserver -k`` cooldown dedupes a cancel + Stop for ONE wineserver, not launches."""

from __future__ import annotations

import pytest

from nadare.backends import wine
from nadare.core.events import Cancelled


class _Done:
    returncode = 0


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner = tmp_path / "runner"
    (runner / "files" / "bin").mkdir(parents=True)
    server = runner / "files" / "bin" / "wineserver"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    calls: list[list[str]] = []
    clock = [1000.0]
    monkeypatch.setattr(wine.subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done())
    monkeypatch.setattr(wine.time, "monotonic", lambda: clock[0])
    wine._WINESERVER_KILLED.clear()

    def env(spelling=None):
        return {"PROTONPATH": str(runner), "WINEPREFIX": str(spelling or prefix)}

    return SimpleRig(runner, prefix, calls, clock, env, tmp_path)


class SimpleRig:
    def __init__(self, runner, prefix, calls, clock, env, tmp_path):
        self.runner, self.prefix, self.calls, self.clock, self.env, self.tmp = (
            runner, prefix, calls, clock, env, tmp_path,
        )


def _cancelled_launch(rig, monkeypatch):
    def boom(*_a, **_k):
        raise Cancelled()

    monkeypatch.setattr(wine.process, "run", boom)
    with pytest.raises(Cancelled):
        wine.stream([str(rig.runner)], rig.env())


def test_a_cancel_and_the_stop_behind_it_still_kill_once(rig, monkeypatch):
    _cancelled_launch(rig, monkeypatch)
    rig.clock[0] += 0.5
    wine._stop_wineserver([str(rig.runner)], rig.env())  # the queued Stop
    assert len(rig.calls) == 1


def test_a_new_launch_that_is_cancelled_is_not_skipped(rig, monkeypatch):
    _cancelled_launch(rig, monkeypatch)
    rig.clock[0] += 0.5  # relaunch and cancel again right away
    _cancelled_launch(rig, monkeypatch)
    assert len(rig.calls) == 2, "the second launch has its own wineserver"


def test_one_prefix_under_two_spellings_shares_one_cooldown(rig):
    link = rig.tmp / "link"
    link.symlink_to(rig.prefix)
    wine._stop_wineserver([str(rig.runner)], rig.env())
    rig.clock[0] += 0.5
    wine._stop_wineserver([str(rig.runner)], rig.env(link))
    assert len(rig.calls) == 1


def test_the_cooldown_still_ends(rig):
    wine._stop_wineserver([str(rig.runner)], rig.env())
    rig.clock[0] += wine._WINESERVER_KILL_COOLDOWN + 0.1
    wine._stop_wineserver([str(rig.runner)], rig.env())
    assert len(rig.calls) == 2


def test_a_stream_without_an_owned_prefix_resets_nothing_and_does_not_crash(rig, monkeypatch):
    wine._stop_wineserver([str(rig.runner)], rig.env())
    monkeypatch.setattr(wine.process, "run", lambda *a, **k: _Done())
    wine.stream(["true"], {})
    rig.clock[0] += 0.5
    wine._stop_wineserver([str(rig.runner)], rig.env())
    assert len(rig.calls) == 1, "a stream that is not about this prefix leaves its cooldown alone"
