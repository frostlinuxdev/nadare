# SPDX-License-Identifier: GPL-3.0-or-later
"""#780: loose-file installs report one overall progress, not every file's own counter."""

from __future__ import annotations

from nadare.core import directdl as dd
from nadare.core.events import Event


def _plan(sizes):
    files = tuple(
        dd.RemoteFile(f"f{i}.bin", f"https://example.invalid/{i}", s) for i, s in enumerate(sizes)
    )
    return dd.Plan("T", "1", files=files), files


def _run(tmp_path, monkeypatch, sizes, chunks=50, workers=6, skip=()):
    plan, files = _plan(sizes)
    monkeypatch.setattr(dd, "plan_missing", lambda *a, **k: (list(files), []))

    def fake(rf, dest, *, emit=None, cancel=None, pool=None):
        index = int(rf.path[1:-4])
        if index in skip:
            return  # already on disk and verified: no progress events at all
        done = 0
        step = max(1, rf.size // chunks)
        while done < rf.size:
            done = min(rf.size, done + step)
            emit(
                Event(
                    "progress",
                    f"download: {done}/{rf.size}",
                    progress=done / rf.size,
                    data={"stage": "download", "done": done, "total": rf.size},
                )
            )
        emit(Event("step", f"got {rf.path}"))

    monkeypatch.setattr(dd, "_download_remote", fake)
    seen: list[Event] = []
    dd.sync(plan, tmp_path, emit=seen.append, workers=workers)
    progress = [e for e in seen if e.kind == "progress"]
    return seen, progress, sum(sizes)


def test_many_small_files_give_one_overall_total(tmp_path, monkeypatch):
    sizes = [7000 + i for i in range(120)]
    _seen, progress, total = _run(tmp_path, monkeypatch, sizes)
    assert progress
    assert {e.data["total"] for e in progress} == {total}


def test_overall_progress_never_goes_backwards(tmp_path, monkeypatch):
    sizes = [100_000, 3_000, 2_000_000, 500, 40_000] * 20
    _seen, progress, _total = _run(tmp_path, monkeypatch, sizes)
    fractions = [e.progress for e in progress]
    dones = [e.data["done"] for e in progress]
    assert fractions == sorted(fractions)
    assert dones == sorted(dones)


def test_progress_is_throttled(tmp_path, monkeypatch):
    sizes = [50_000] * 200  # 10,000 per-file events in well under a second
    _seen, progress, _total = _run(tmp_path, monkeypatch, sizes)
    assert len(progress) <= 8


def test_the_last_event_is_the_complete_total(tmp_path, monkeypatch):
    sizes = [12_345] * 30
    _seen, progress, total = _run(tmp_path, monkeypatch, sizes)
    assert progress[-1].data["done"] == total
    assert progress[-1].progress == 1.0


def test_files_already_on_disk_still_count_toward_the_total(tmp_path, monkeypatch):
    sizes = [10_000] * 10
    _seen, progress, total = _run(tmp_path, monkeypatch, sizes, skip={0, 3})
    assert progress[-1].data["done"] == total


def test_other_events_still_reach_the_log(tmp_path, monkeypatch):
    seen, _progress, _total = _run(tmp_path, monkeypatch, [1000] * 5)
    assert sum(1 for e in seen if e.kind == "step" and e.message.startswith("got ")) == 5
