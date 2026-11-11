# SPDX-License-Identifier: GPL-3.0-or-later
"""#831 part 4: comparing tens of thousands of files says so, instead of a silent 0 %."""

from __future__ import annotations

from nadare.core import directdl as dd
from nadare.core.events import Event


def _plan(count: int) -> dd.Plan:
    files = tuple(
        dd.RemoteFile(path=f"data/f{i}.bin", url=f"https://cdn.example/f{i}.bin", size=1)
        for i in range(count)
    )
    return dd.Plan(game_tag="t", version="1", files=files, exe="game.exe")


def test_plan_missing_reports_how_many_files_it_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_CHECK_PROGRESS_INTERVAL", 0.0)
    seen: list[tuple[int, int]] = []
    missing, _archives = dd.plan_missing(
        _plan(40), tmp_path, progress=lambda a, b: seen.append((a, b))
    )
    assert len(missing) == 40
    assert seen and seen[-1] == (40, 40)
    assert all(total == 40 for _done, total in seen)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


def test_sync_says_it_is_checking_files_before_it_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_CHECK_PROGRESS_INTERVAL", 0.0)
    events: list[Event] = []
    plan = _plan(5)
    for rf in plan.files:  # everything already on disk: nothing to download
        path = tmp_path / rf.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    dd.sync(plan, tmp_path, emit=events.append)
    checking = [e for e in events if e.kind == "step" and "Checking" in (e.message or "")]
    assert checking, [e.message for e in events]
    assert "5 of 5" in checking[-1].message
    assert not any(e.kind == "progress" and "Checking" in (e.message or "") for e in events)
