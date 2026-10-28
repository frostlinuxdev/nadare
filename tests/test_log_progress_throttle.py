# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-run log keeps a sample of download progress, not a line per megabyte."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui import workers


def _worker(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    clock = {"now": 1000.0}
    monkeypatch.setattr(workers.time, "monotonic", lambda: clock["now"])
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "G", "install")
    return worker, logs / "G-install.log", clock


def _progress(done: int, total: int = 1000) -> Event:
    return Event("progress", f"install: {done} / {total}", progress=done / total)


def _lines(path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if "[progress]" in line]


def test_a_stream_of_progress_events_is_sampled_in_the_log(tmp_path, monkeypatch):
    worker, path, clock = _worker(tmp_path, monkeypatch)
    seen = []
    worker.backend_event.connect(seen.append)
    for step in range(1, 601):  # ten minutes, one event per second
        clock["now"] += 1.0
        worker._on_event(_progress(step))
    worker._close_log()
    assert len(seen) == 600, "the UI still gets every event"
    lines = _lines(path)
    assert 15 <= len(lines) <= 25, len(lines)  # about one per 30 s, plus the first


def test_the_finished_progress_line_is_always_logged(tmp_path, monkeypatch):
    worker, path, clock = _worker(tmp_path, monkeypatch)
    worker._on_event(_progress(10))
    clock["now"] += 1.0
    worker._on_event(_progress(999))  # throttled
    clock["now"] += 1.0
    worker._on_event(_progress(1000))  # complete: never dropped
    worker._close_log()
    lines = _lines(path)
    assert "10 / 1000" in lines[0]
    assert "1000 / 1000" in lines[-1]
    assert not any("999 / 1000" in line for line in lines)


def test_other_events_are_never_throttled(tmp_path, monkeypatch):
    worker, path, clock = _worker(tmp_path, monkeypatch)
    for index in range(5):
        worker._on_event(Event("step", f"step {index}"))
    worker._close_log()
    text = path.read_text(encoding="utf-8")
    assert all(f"[step] step {index}" in text for index in range(5))
