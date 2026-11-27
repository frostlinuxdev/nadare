# SPDX-License-Identifier: GPL-3.0-or-later
"""Runner rows, transfer rates and queued GUI delivery (#1625, #1623)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication
from test_downloads_window import _close, _start, _window, no_real_art  # noqa: F401

from nadare.core import runners
from nadare.core.events import Event

TAG = "dwproton-11.0-17"
KEY = f"runner:{TAG}"
MI = 1024**2


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def runner_event(kind="progress", progress=0.5, **data):
    return Event(kind, "Downloading runner", progress, {
        "runner": "dwproton", "tag": TAG, "total": 100 * MI,
        **data,
    })


def test_speed_is_measured_over_about_three_seconds_and_resets():
    from nadare.core.eta import SpeedMeter

    meter = SpeedMeter(window=3.0)
    assert meter.update(0, 0) is None
    assert meter.update(MI, 1) == MI
    assert meter.update(9 * MI, 2) == 4.5 * MI
    assert meter.update(12 * MI, 3) == 4 * MI
    assert meter.update(17 * MI, 4) == pytest.approx(16 * MI / 3)
    assert meter.update(0, 5) is None
    assert meter.update(MI, 70) is None


@pytest.mark.parametrize("byte_data", [{"done": 20 * MI}, {}])
def test_game_speed_fraction_fallback_connections_and_reset(
    tmp_path, qapp, monkeypatch, byte_data,
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr(
            "nadare.gui.main_window_parts.downloads.time.monotonic", lambda: clock[0]
        )
        window._on_event("a", Event("progress", progress=0.1,
                                    data={"done": 10 * MI, "total": 100 * MI}))
        clock[0] = 11.0
        window._on_event("a", Event("progress", progress=0.2,
                                    data={"total": 100 * MI, "connections": 4, **byte_data}))
        assert "10.0 MiB/s · 4 connections" in window.downloads.row("a").eta_label.text()
        window.show_downloads()  # rebuilding preserves the rate
        assert "10.0 MiB/s" in window.downloads.row("a").eta_label.text()
        window._on_busy("a", "install", True)
        window.show_downloads()
        assert "MiB/s" not in window.downloads.row("a").eta_label.text()
        # A new run starts its own rate: the old samples must not turn its first bytes into
        # a spike.
        window._on_event("a", Event("progress", progress=0.3,
                                    data={"done": 400 * MI, "total": 1000 * MI}))
        assert "MiB/s" not in window.downloads.row("a").eta_label.text()
    finally:
        _close(window, qapp)


def test_game_connections_stay_when_the_speed_momentarily_disappears(
    tmp_path, qapp, monkeypatch,
):
    """A tick with no speed yet still shows the connections it knows about."""
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr(
            "nadare.gui.main_window_parts.downloads.time.monotonic", lambda: clock[0]
        )
        # The first tick has no rate yet; it does know the connections.
        window._on_event("a", Event("progress", progress=0.1,
                                    data={"done": 10 * MI, "total": 100 * MI,
                                          "connections": 32}))
        assert "32 connections" in window.downloads.row("a").eta_label.text()
        clock[0] = 11.0
        # The next tick measures a speed and does not repeat the count.
        window._on_event("a", Event("progress", progress=0.2,
                                    data={"done": 20 * MI, "total": 100 * MI}))
        text = window.downloads.row("a").eta_label.text()
        assert "MiB/s" in text
        assert "32 connections" in text  # kept from the last tick
    finally:
        _close(window, qapp)


def test_runner_connections_stay_when_the_speed_momentarily_disappears(
    tmp_path, qapp, monkeypatch,
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr(
            "nadare.gui.main_window_parts.downloads.time.monotonic", lambda: clock[0]
        )
        # The first tick has no rate yet; it does know the connections.
        window._on_event("a", runner_event(progress=0.1, done=10 * MI, connections=8))
        assert "8 connections" in window.downloads.row(KEY).eta_label.text()
        clock[0] = 11.0
        window._on_event("a", runner_event(progress=0.2, done=20 * MI))
        text = window.downloads.row(KEY).eta_label.text()
        assert "MiB/s" in text
        assert "8 connections" in text  # kept from the last tick
    finally:
        _close(window, qapp)


def test_runner_grouping_updates_terminal_timer_and_no_game_progress(
    tmp_path, qapp, monkeypatch,
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr(
            "nadare.gui.main_window_parts.downloads.time.monotonic", lambda: clock[0]
        )
        window._on_event("a", runner_event("status", None))
        window._on_event("a", runner_event(progress=0.1, done=10 * MI))
        clock[0] = 11.0
        window._on_event("a", runner_event(done=50 * MI))
        assert window.downloads.row_profiles() == ["a", KEY, "b"]
        row = window.downloads.row(KEY)
        assert row.title_label.text() == "DW-Proton 11.0-17"
        assert row.bar.value() == 50
        assert row.status_label.text() == "Downloading 50 MiB of 100 MiB"
        assert "40.0 MiB/s" in row.eta_label.text()
        assert "less than a minute left" in row.eta_label.text()
        assert not row.pause_btn.isVisibleTo(window.downloads)
        game_left = window.downloads.row("a").layout().contentsMargins().left()
        assert row.layout().contentsMargins().left() > game_left
        assert window.downloads.row("a").bar.maximum() == 0
        timers = []
        monkeypatch.setattr("nadare.gui.main_window_parts.downloads.QTimer.singleShot",
                            lambda delay, callback: timers.append((delay, callback)))
        # A verified archive is not an installed runner.
        window._on_event("a", runner_event("done", 1.0))
        assert not timers
        window._on_event("a", runner_event("done", None, runner_finished=True))
        assert window.downloads.row(KEY).status_label.text() == "Finished"
        assert not row.cancel_btn.isVisibleTo(window.downloads)
        assert 1000 <= timers[0][0] <= 10000
        timers[0][1]()
        assert window.downloads.row_profiles() == ["a", "b"]
    finally:
        _close(window, qapp)


def test_runner_titles_keep_the_family_name():
    from nadare.gui.main_window_parts.downloads import _runner_title

    assert _runner_title("ge", "GE-Proton11-7") == "GE-Proton 11-7"
    assert _runner_title("cachyos", "cachyos-11.0-20260703-slr") == (
        "Proton-CachyOS 11.0-20260703-slr"
    )
    assert _runner_title("dwproton", "dwproton-11.0-17") == "DW-Proton 11.0-17"


def test_launch_runner_stands_alone_and_cancel_stops_owner(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "launch")
        window.show_downloads()
        window._on_event("a", runner_event())
        assert window.downloads.row_profiles() == [KEY]
        assert window.downloads_button.text() == "Downloads (1)"
        # A runner row is not a game: clicking it must not open a page.
        window.downloads.row(KEY).clicked.emit()
        assert window.stack.currentWidget() is window.downloads
        calls = []
        monkeypatch.setattr(window.controller, "cancel", calls.append)
        window.downloads.row(KEY).cancel_btn.click()
        assert calls == ["a"]
        window._on_busy("a", "launch", False)
        assert window.downloads.row_profiles() == []
    finally:
        _close(window, qapp)


def test_prefetch_emit_is_queued_cancel_retries_and_no_widgets_in_thread(
    tmp_path, qapp, monkeypatch,
):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        tokens = []
        release = threading.Event()
        emitted = threading.Event()
        gui_threads = []
        original = window.downloads.set_rows

        def set_rows(rows):
            gui_threads.append(QThread.currentThread())
            original(rows)

        monkeypatch.setattr(window.downloads, "set_rows", set_rows)

        def prefetch(profiles, *, emit, cancel, **_):
            tokens.append(cancel)
            emit(runner_event())
            emitted.set()
            release.wait(5)
            cancel.raise_if_cancelled()
            return []

        monkeypatch.setattr(runners, "prefetch", prefetch)
        window._real_start_runner_prefetch()
        assert emitted.wait(2)
        assert window.downloads.row_profiles() == []  # emitter never touches widgets
        qapp.processEvents()
        assert window.downloads.row_profiles() == [KEY]
        window.downloads.row(KEY).cancel_btn.click()
        assert tokens[0].cancelled
        assert window.downloads.row_profiles() == []  # the row goes at once
        release.set()
        window._runner_prefetch_thread.join(2)
        qapp.processEvents()
        assert window.downloads.row_profiles() == []
        assert gui_threads and all(t == window.thread() for t in gui_threads)
        # A later prefetch has its own fresh token and can succeed.
        monkeypatch.setattr(
            runners, "prefetch", lambda p, *, emit, cancel, **_: tokens.append(cancel) or []
        )
        window._real_start_runner_prefetch()
        window._runner_prefetch_thread.join(2)
        qapp.processEvents()
        assert len(tokens) == 2 and not tokens[1].cancelled
    finally:
        release.set()
        _close(window, qapp)


def test_a_finished_prefetch_row_lingers_then_leaves(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        timers = []
        monkeypatch.setattr(
            "nadare.gui.main_window_parts.downloads.QTimer.singleShot",
            lambda delay, callback: timers.append((delay, callback)),
        )

        def prefetch(profiles, *, emit, cancel, **_):
            emit(runner_event("status", None))
            emit(runner_event(progress=0.5, done=50_000_000))
            emit(runner_event("done", 1.0, runner_finished=True))
            return []

        monkeypatch.setattr(runners, "prefetch", prefetch)
        window._real_start_runner_prefetch()
        window._runner_prefetch_thread.join(2)
        qapp.processEvents()
        # The prefetch ending does not drop a row that finished normally: it keeps its timer.
        assert window.downloads.row_profiles() == [KEY]
        assert window.downloads.row(KEY).status_label.text() == "Finished"
        assert len(timers) == 1 and 1000 <= timers[0][0] <= 10000
        timers[0][1]()
        assert window.downloads.row_profiles() == []
    finally:
        _close(window, qapp)


def test_runner_install_labels_every_event_and_only_finishes_after_extract(tmp_path, monkeypatch):
    tar = tmp_path / "runner.tar.gz"
    tar.write_bytes(b"fixture")
    monkeypatch.setattr(runners.paths, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(runners, "installed_pinned", lambda _: None)
    monkeypatch.setattr(runners.diskspace, "check_space", lambda *a: None)
    monkeypatch.setattr(runners, "_pinned_release", lambda spec: {
        "tag": TAG, "tar": {"name": tar.name, "size": 7,
        "browser_download_url": "https://github.com/fixture/runner"}, "sum": None,
    })
    monkeypatch.setattr(runners, "_reuse_verified", lambda *a, **k: None)

    def download(*a, emit, **k):
        emit(Event("progress", progress=0.5, data={"done": 3, "total": 7}))
        emit(Event("done", "verified archive"))
        return SimpleNamespace(path=tar)

    def extract(*a, emit, **k):
        emit(Event("status", "Unpacking", data={"stage": "extract"}))
        return tmp_path / TAG

    monkeypatch.setattr(runners.download, "download", download)
    monkeypatch.setattr(runners, "_extract", extract)
    events = []
    runners.install("dwproton", emit=events.append)
    assert all(e.data.get("runner") == "dwproton" and e.data.get("tag") == TAG for e in events)
    assert events[0].data["total"] == 7
    # One formatter: the status line is human_size, not a hand-made "0 MB".
    assert runners.diskspace.human_size(7) in events[0].message
    assert " MB)" not in events[0].message
    assert [e for e in events if e.data.get("runner_finished")] == [events[-1]]


def test_byte_only_progress_replaces_the_rate_instead_of_appending_it(
    tmp_path, qapp, monkeypatch,
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr("nadare.gui.main_window_parts.downloads.time.monotonic",
                            lambda: clock[0])
        for index in range(3):
            clock[0] = 10.0 + index
            window._on_event("a", Event("progress", data={
                "done": (index + 1) * 10 * MI, "total": 100 * MI,
            }))
        assert window.downloads.row("a").eta_label.text() == "10.0 MiB/s"
    finally:
        _close(window, qapp)


def test_fraction_progress_uses_the_total_from_the_initial_status(
    tmp_path, qapp, monkeypatch,
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr("nadare.gui.main_window_parts.downloads.time.monotonic",
                            lambda: clock[0])
        window._on_event("a", Event("status", data={"total": 100 * MI}))
        window._on_event("a", Event("progress", progress=0.1))
        clock[0] += 1
        window._on_event("a", Event("progress", progress=0.2))
        assert "10.0 MiB/s" in window.downloads.row("a").eta_label.text()
    finally:
        _close(window, qapp)


def test_unpacking_runner_clears_the_download_rate_and_eta(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        clock = [10.0]
        monkeypatch.setattr("nadare.gui.main_window_parts.downloads.time.monotonic",
                            lambda: clock[0])
        window._on_runner_download_event("a", runner_event(done=10 * MI))
        clock[0] += 1
        window._on_runner_download_event("a", runner_event(done=20 * MI))
        assert "MiB/s" in window.downloads.row(KEY).eta_label.text()
        window._on_runner_download_event("a", runner_event("status", None, stage="extract"))
        assert window.downloads.row(KEY).status_label.text() == "Unpacking"
        assert window.downloads.row(KEY).eta_label.text() == ""
    finally:
        _close(window, qapp)


def test_unpacking_a_runner_turns_its_full_bar_busy(tmp_path, qapp, monkeypatch):
    """The runner transfer is at 100%, but extracting has no percentage of its own."""
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        window._on_runner_download_event(
            "a", runner_event(progress=1.0, done=100 * MI)
        )
        assert window.downloads.row(KEY).bar.maximum() == 100
        window._on_runner_download_event("a", runner_event("status", None, stage="extract"))
        assert window.downloads.row(KEY).status_label.text() == "Unpacking"
        assert window.downloads.row(KEY).bar.maximum() == 0
    finally:
        _close(window, qapp)


def test_cancelled_source_cannot_hide_a_fresh_download_of_the_same_runner(
    tmp_path, qapp, monkeypatch,
):
    from nadare.core.events import Cancel

    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        old, new = Cancel(), Cancel()
        window._on_runner_download_event(old, runner_event())
        window._cancel_download_row(KEY)
        window._on_runner_download_event(new, runner_event(progress=0.2))
        assert window.downloads.row(KEY) is not None
        window._on_runner_download_event(old, runner_event(progress=0.9))
        assert window.downloads.row(KEY).bar.value() == 20
        window._on_runner_download_event(old, Event("runner_end"))
        # Already queued events from the cancelled source must stay ignored after its end.
        window._on_runner_download_event(old, runner_event(progress=0.9))
        assert window.downloads.row(KEY).bar.value() == 20
        window._cancel_download_row(KEY)
        assert new.cancelled
    finally:
        _close(window, qapp)


def test_shared_runner_stays_visible_when_one_waiting_game_ends(tmp_path, qapp, monkeypatch):
    from nadare.core.events import Cancel

    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        token = Cancel()
        window._on_runner_download_event(token, runner_event())
        window._on_runner_download_event("a", runner_event("status", None))
        window._end_runner_downloads("a")
        assert window.downloads.row(KEY) is not None  # prefetch still owns the transfer
        assert window.downloads.row_profiles() == ["a", KEY]
        window._on_runner_download_event(token, Event("runner_end"))
        assert window.downloads.row(KEY) is None
    finally:
        _close(window, qapp)


def test_shared_runner_stays_visible_when_prefetch_ends_before_play(tmp_path, qapp, monkeypatch):
    from nadare.core.events import Cancel

    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        token = Cancel()
        window._on_runner_download_event(token, runner_event())
        window._on_runner_download_event("a", runner_event("status", None))
        window._on_runner_download_event(token, Event("runner_end"))
        assert window.downloads.row(KEY) is not None  # Play can retry after the lock releases
        calls = []
        monkeypatch.setattr(window.controller, "cancel", calls.append)
        window._cancel_download_row(KEY)
        assert calls == ["a"]
        window._end_runner_downloads("a")
        assert window.downloads.row(KEY) is None
    finally:
        _close(window, qapp)


def test_cancelling_a_shared_transfer_stops_every_owner_and_ignores_queued_events(
    tmp_path, qapp, monkeypatch,
):
    from nadare.core.events import Cancel

    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        token = Cancel()
        window._on_runner_download_event(token, runner_event())
        window._on_runner_download_event("a", runner_event("status", None))
        window._on_runner_download_event("b", runner_event("status", None))
        calls = []
        monkeypatch.setattr(window.controller, "cancel", calls.append)
        window._cancel_download_row(KEY)
        assert token.cancelled
        assert sorted(calls) == ["a", "b"]
        assert window.downloads.row(KEY) is None
        window._on_runner_download_event("a", runner_event())
        window._on_runner_download_event("b", runner_event())
        assert window.downloads.row(KEY) is None
        window._end_runner_downloads("a")
        window._end_runner_downloads("b")
        window._on_runner_download_event(token, Event("runner_end"))
        window._on_runner_download_event("a", runner_event())
        assert window.downloads.row(KEY) is not None  # a later game operation can retry
    finally:
        _close(window, qapp)
