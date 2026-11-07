# SPDX-License-Identifier: GPL-3.0-or-later
"""The progress bar and the "download: X / Y" text must always describe the same thing.

A loose-file update (Punishing: Gray Raven) downloads hundreds of files in parallel. Before the
overall counter (#780) each file reported its own ``done / total``, so the bar could sit at 100%
beside a text that jumped between 47 MiB and 53 MiB.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import directdl as dd
from nadare.core.download import human_size
from nadare.core.events import Event
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("sample", {"title": "Sample"})
    yield page
    page.deleteLater()
    qapp.processEvents()


def _sync_into(view, tmp_path, monkeypatch, sizes, workers=6):
    files = tuple(
        dd.RemoteFile(f"f{i}.bin", f"https://example.invalid/{i}", s) for i, s in enumerate(sizes)
    )
    plan = dd.Plan("T", "1", files=files)
    monkeypatch.setattr(dd, "plan_missing", lambda *a, **k: (list(files), []))

    def fake(rf, dest, *, emit=None, cancel=None, pool=None):
        done = 0
        step = max(1, rf.size // 20)
        while done < rf.size:
            done = min(rf.size, done + step)
            emit(
                Event(
                    "progress",
                    f"download: {done} / {rf.size}",
                    progress=done / rf.size,
                    data={"stage": "download", "done": done, "total": rf.size},
                )
            )
        emit(Event("log", f"verified {rf.path} ({rf.size} bytes)"))

    monkeypatch.setattr(dd, "_download_remote", fake)
    events: list[Event] = []
    dd.sync(plan, tmp_path, emit=events.append, workers=workers)
    return events


def test_bar_and_text_agree_on_every_event(view, tmp_path, monkeypatch):
    sizes = [47 * 1024, 4_300, 6_000_000 // 100, 900, 53 * 1024] * 12
    events = _sync_into(view, tmp_path, monkeypatch, sizes)
    checked = 0
    for event in events:
        view.append_event(event)
        if event.kind != "progress" or event.progress is None:
            continue
        done, total = event.data["done"], event.data["total"]
        assert view.progress.value() == max(0, min(100, int(round(done / total * 100))))
        # the speed, when there is one, follows the same "done / total" after a separator
        assert view.step_label.text().split("  ·  ")[0] == event.message
        checked += 1
    assert checked >= 2


def test_bar_is_not_full_until_everything_is_downloaded(view, tmp_path, monkeypatch):
    sizes = [50_000, 3_000, 2_000_000, 500, 40_000] * 8
    events = _sync_into(view, tmp_path, monkeypatch, sizes)
    total = sum(sizes)
    for event in events:
        view.append_event(event)
        if event.kind == "progress" and event.data["done"] < total:
            assert view.progress.value() < 100 or event.data["done"] / total >= 0.995
    last = [e for e in events if e.kind == "progress"][-1]
    assert last.data["done"] == total
    assert view.progress.value() == 100


def test_text_totals_are_one_number(view, tmp_path, monkeypatch):
    sizes = [7000 + i for i in range(60)]
    events = _sync_into(view, tmp_path, monkeypatch, sizes)
    totals = set()
    for event in events:
        view.append_event(event)
        if event.kind == "progress":
            totals.add(view.step_label.text().split("  ·  ")[0].rsplit("/ ", 1)[-1])
    assert totals == {human_size(sum(sizes))}


def test_the_download_line_shows_the_speed(view, monkeypatch):
    import time

    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    total = 80 * 1024**3
    mib = 1024**2
    for second in range(0, 6):
        clock["now"] = 100.0 + second
        done = second * 85 * mib
        view.append_event(
            Event(
                "progress",
                f"download: {human_size(done)} / {human_size(total)}",
                progress=done / total,
                data={"stage": "download", "done": done, "total": total},
            )
        )
    text = view.step_label.text()
    assert text.startswith("download: ") and "85.0 MiB/s" in text


def test_a_progress_line_without_byte_counts_gets_no_speed(view):
    view.append_event(Event("progress", "installing runtime", progress=0.5))
    assert view.step_label.text() == "installing runtime"
