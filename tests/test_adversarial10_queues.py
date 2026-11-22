# SPDX-License-Identifier: GPL-3.0-or-later
"""Queue schedules retain capacity, cancellation and at-most-once execution."""

from __future__ import annotations

import json
import random

import pytest
from test_install_queue_order import FakeWorker

from nadare.core.config import Config
from nadare.gui.workers import BackendController


@pytest.mark.parametrize(
    "value",
    [None, "nonsense", [], {}, -100, 0, 1, 2, 999999, float("nan"), float("inf"), float("-inf")],
)
def test_malformed_concurrency_setting_cannot_break_queue(tmp_path, qtbot, value):
    path = tmp_path / "config.json"
    encoded = json.dumps({"profiles": {}, "max_concurrent_installs": value})
    path.write_text(encoded.replace("Infinity", "1e309"))
    cfg = Config.load(path)
    ctl = BackendController(cfg)
    assert 1 <= ctl.max_active_installs() <= 4


@pytest.mark.parametrize("seed", range(20))
def test_queue_reorder_cancel_and_finish_schedule(tmp_path, qtbot, seed):
    rng = random.Random(seed)
    cfg = Config.load(tmp_path / "config.json")
    cfg.data["max_concurrent_installs"] = 1 + seed % 2
    names = list("abcdefgh")
    for name in names:
        cfg.add_profile(name, backend="wine", title=name)
    rng.shuffle(names)
    ctl = BackendController(cfg)
    spawned = []

    def spawn(profile, operation, payload=None):
        ctl._workers[profile] = FakeWorker(operation, payload)
        spawned.append(profile)
        return True

    ctl._spawn = spawn
    try:
        for name in names:
            assert ctl.start(name, "install")
            assert len(ctl._workers) <= cfg.data["max_concurrent_installs"]
        waiting = list(ctl._waiting)
        cancelled = set(rng.sample(waiting, 2))
        for name in cancelled:
            ctl.cancel(name)
        remaining = [name for name in waiting if name not in cancelled]
        ctl.move_to_front(remaining[-1])
        for _ in range(20):
            if not ctl._workers:
                break
            name = next(iter(ctl._workers))
            worker = ctl._workers[name]
            worker._running = False
            ctl._on_succeeded(name, "install", 0, worker)
            assert len(ctl._workers) <= cfg.data["max_concurrent_installs"]
        assert not ctl._waiting and not ctl._workers
        assert set(spawned) == set(names) - cancelled
        assert len(spawned) == len(set(spawned))
    finally:
        ctl._workers.clear()
        ctl._waiting.clear()
