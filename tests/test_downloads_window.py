# SPDX-License-Identifier: GPL-3.0-or-later
"""The Downloads page in the main window: sidebar entry, live rows and working buttons (#773)."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui.main_window import PAGE_DOWNLOADS, PAGE_GAME, MainWindow
from nadare.gui.workers import BackendController


class FakeWorker:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.payload = None

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def cancel(self) -> None:
        pass

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _window(tmp_path: Path, monkeypatch) -> MainWindow:
    def fake_spawn(self, profile, operation, payload=None):
        if operation in ("status", "news", "media", "check_update"):
            return True  # background probes (started while the window is built) do not matter
        self._workers[profile] = FakeWorker(operation)
        self.busy.emit(profile, operation, True)
        return True

    monkeypatch.setattr(BackendController, "_spawn", fake_spawn)
    config = Config.load(tmp_path / "config.json")
    for name in ("a", "b", "c"):
        config.add_profile(name, backend="direct-pgr", title=name.upper())
    config.add_profile("w", backend="wine", title="W")
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    window.controller._workers.clear()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _start(window, *names):
    for name in names:
        window.controller.start(name, "install")


def _pills(row) -> list[str]:
    """Every visible state badge the row shows (a row must show exactly one)."""
    return [
        label.text()
        for label in row.findChildren(QLabel)
        if label.objectName() == "Pill" and label.isVisibleTo(row)
    ]


def _badge(row) -> QLabel:
    """The row's one visible state badge, and a check that there is exactly one."""
    labels = [
        label
        for label in row.findChildren(QLabel)
        if label.objectName() == "Pill" and label.isVisibleTo(row)
    ]
    assert len(labels) == 1
    return labels[0]


def _visible_label_texts(row) -> list[str]:
    return [label.text() for label in row.findChildren(QLabel) if label.isVisibleTo(row)]


def test_the_sidebar_has_a_downloads_entry_that_opens_the_page(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        assert window.downloads_button.text().startswith("Downloads")
        window.downloads_button.click()
        assert window.stack.currentIndex() == PAGE_DOWNLOADS
        assert window.stack.currentWidget() is window.downloads
        assert window.downloads_button.property("active") == "true"
        assert window.library_button.property("active") == "false"
    finally:
        _close(window, qapp)


def test_the_page_is_empty_until_something_downloads(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        assert window.downloads.row_profiles() == []
        assert window.downloads_button.text() == "Downloads"
    finally:
        _close(window, qapp)


def test_running_and_waiting_installs_are_listed_in_order_with_a_count(
    tmp_path, qapp, monkeypatch
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b", "c")
        window.show_downloads()
        assert window.downloads.row_profiles() == ["a", "b", "c"]
        assert window.downloads.row("a").pause_btn.isVisibleTo(window.downloads)
        assert "Next in line" in window.downloads.row("b").status_label.text()
        assert "1 install ahead" in window.downloads.row("c").status_label.text()
        assert window.downloads_button.text() == "Downloads (3)"
    finally:
        _close(window, qapp)


def test_a_wine_install_has_no_pause_because_it_cannot_resume(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "w", "b")
        window.show_downloads()
        assert not window.downloads.row("w").pause_btn.isVisibleTo(window.downloads)
        assert not window.downloads.row("b").now_btn.isVisibleTo(window.downloads)
    finally:
        _close(window, qapp)


def test_the_list_follows_the_queue_while_the_page_is_open(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        _start(window, "a")
        assert window.downloads.row_profiles() == ["a"]
        _start(window, "b")
        assert window.downloads.row_profiles() == ["a", "b"]
        window.controller._on_succeeded("a", "install", 0, window.controller._workers["a"])
        assert window.downloads.row_profiles() == ["b"]
        assert window.downloads_button.text() == "Downloads (1)"
    finally:
        _close(window, qapp)


def test_download_next_reorders_the_line(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b", "c")
        window.show_downloads()
        window.downloads.row("c").next_btn.click()
        assert window.controller.queue_order() == ["c", "b"]
        assert window.downloads.row_profiles() == ["a", "c", "b"]
    finally:
        _close(window, qapp)


def test_download_now_pauses_the_running_one_and_swaps_them(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.row("b").now_btn.click()
        assert "a" in window.controller._pausing
        worker = window.controller._workers["a"]
        window.controller._on_cancelled("a", "install", worker)
        assert window.downloads.row_profiles() == ["b", "a"]
        assert window.downloads.row("b").bar.isVisibleTo(window.downloads)  # b runs now
        assert "Next in line" in window.downloads.row("a").status_label.text()
    finally:
        _close(window, qapp)


def test_pause_and_resume_work_from_the_list(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.row("b").pause_btn.click()  # a waiting download can be paused too
        assert window.controller.is_paused("b")
        assert window.downloads.row("b").resume_btn.isVisibleTo(window.downloads)
        window.downloads.row("b").resume_btn.click()
        assert window.controller.is_waiting("b")
        assert not window.controller.is_paused("b")
    finally:
        _close(window, qapp)


def test_cancel_removes_the_row(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.row("b").cancel_btn.click()
        assert window.downloads.row_profiles() == ["a"]
        assert not window.controller.is_waiting("b")
    finally:
        _close(window, qapp)


def test_cancelling_a_paused_row_removes_it(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.row("b").pause_btn.click()
        assert window.controller.is_paused("b")
        window.downloads.row("b").cancel_btn.click()
        assert not window.controller.is_paused("b")
        assert window.downloads.row_profiles() == ["a"]  # the paused row must not linger
    finally:
        _close(window, qapp)


def test_clicking_a_row_opens_that_game(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.open_requested.emit("b")
        assert window.stack.currentIndex() == PAGE_GAME
        assert window._current_profile == "b"
    finally:
        _close(window, qapp)


def test_progress_events_move_the_bar_of_the_running_row(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window.show_downloads()
        window._on_event("a", Event("progress", "Downloading 5 GiB of 10 GiB", progress=0.5))
        row = window.downloads.row("a")
        assert row.bar.value() == 50
        assert row.status_label.text() == "Downloading 5 GiB of 10 GiB"
    finally:
        _close(window, qapp)


def test_progress_seen_while_the_page_was_closed_shows_when_it_opens(
    tmp_path, qapp, monkeypatch
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window._on_event("a", Event("progress", "Downloading", progress=0.3))
        window.show_downloads()
        assert window.downloads.row("a").bar.value() == 30
    finally:
        _close(window, qapp)


def test_a_new_run_does_not_inherit_the_old_progress(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a")
        window._on_event("a", Event("progress", "x", progress=0.9))
        window.controller._on_succeeded("a", "install", 0, window.controller._workers["a"])
        _start(window, "a")
        window.show_downloads()
        assert window.downloads.row("a").bar.maximum() == 0  # busy: nothing measured yet
    finally:
        _close(window, qapp)


def test_a_game_removed_from_the_library_has_no_row(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        del window.config.data["profiles"]["b"]
        window.show_downloads()
        assert window.downloads.row_profiles() == ["a"]
    finally:
        _close(window, qapp)


@pytest.mark.parametrize(
    ("active", "paused", "text", "tip"),
    [
        (0, 0, "Downloads", ""),
        (3, 0, "Downloads (3)", "3 downloading or waiting"),
        (2, 1, "Downloads (2 · 1 paused)", "2 downloading or waiting, 1 paused"),
        (0, 2, "Downloads (2 paused)", "2 paused"),
    ],
)
def test_the_downloads_label_says_what_it_counts(active, paused, text, tip):
    """#941: the entry counted running and waiting only while the page also lists paused ones."""
    from nadare.gui.downloads_page import downloads_label

    assert downloads_label(active, paused) == (text, tip)


def test_the_entry_mentions_a_paused_download(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.controller.pause("b")
        assert window.downloads_button.text() == "Downloads (1 · 1 paused)"
        assert "paused" in window.downloads_button.toolTip()
    finally:
        _close(window, qapp)


# ---- b29: a row has one state badge, and the queue reads as a queue ----------------


def test_two_installs_show_one_badge_each_and_the_waiter_says_waiting(
    tmp_path, qapp, monkeypatch
):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        assert _pills(window.downloads.row("a")) == ["INSTALLING"]
        assert _pills(window.downloads.row("b")) == ["WAITING"]  # not INSTALLING too
        assert window.downloads.row("b").now_btn.isVisibleTo(window.downloads)
    finally:
        _close(window, qapp)


def test_cancelling_a_running_row_removes_it(tmp_path, qapp, monkeypatch):
    """A row that is running can be cancelled, and the waiting one takes its place."""
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        row = window.downloads.row("a")
        assert _pills(row) == ["INSTALLING"]
        assert not row.now_btn.isVisibleTo(window.downloads)
        monkeypatch.setattr(window, "_ask_cancel_choice", lambda *args: "keep")
        row.cancel_btn.click()
        window.controller._on_cancelled("a", "install", window.controller._workers["a"])
        assert window.downloads.row("a") is None
        assert _pills(window.downloads.row("b")) == ["INSTALLING"]
    finally:
        _close(window, qapp)


def test_a_done_or_cancelled_row_leaves_no_badge_behind(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b", "c")
        window.show_downloads()
        window.downloads.row("c").cancel_btn.click()  # waiting -> cancelled
        assert window.downloads.row_profiles() == ["a", "b"]
        assert _pills(window.downloads.row("b")) == ["WAITING"]
        window.controller._on_succeeded("a", "install", 0, window.controller._workers["a"])
        assert _pills(window.downloads.row("b")) == ["INSTALLING"]  # waiting -> running
        window.controller._on_succeeded("b", "install", 0, window.controller._workers["b"])
        assert window.downloads.row_profiles() == []  # running -> done
    finally:
        _close(window, qapp)


def _assert_inside_viewport(window, row) -> None:
    """Every visible button on the row is fully inside the Downloads viewport."""
    scroll = window.downloads.findChild(QScrollArea)
    viewport = scroll.viewport()
    for button in row.findChildren(QPushButton):
        if not button.isVisibleTo(window.downloads):
            continue
        top_left = button.mapTo(viewport, QPoint(0, 0))
        assert top_left.x() >= 0
        assert top_left.x() + button.width() <= viewport.width()
    row_top_left = row.mapTo(viewport, QPoint(0, 0))
    assert row_top_left.x() >= 0
    assert row_top_left.x() + row.width() <= viewport.width()


def _assert_label_fits(label) -> None:
    shown = label.display_text()
    assert label.fontMetrics().horizontalAdvance(shown) <= label.contentsRect().width()
    if shown == label.text():
        assert label.toolTip() == ""  # fits: no leftover tooltip
    else:
        assert shown.endswith("…")
        assert label.toolTip() == label.text()


@pytest.mark.parametrize("width", [1280, 1024])
def test_the_long_title_and_queue_line_fit_a_four_button_row_in_a_normal_window(
    tmp_path, qapp, monkeypatch, width
):
    window = _window(tmp_path, monkeypatch)
    try:
        for name, title in (
            ("a", "Wuthering Waves"),
            ("b", "Strinova"),
            ("c", "Blue Protocol: Star Resonance"),
        ):
            window.config.profiles[name]["title"] = title
        _start(window, "a", "b", "c")
        window.resize(width, 800)
        window.show()
        window.show_downloads()
        qapp.processEvents()

        # "c" waits behind one install and therefore carries all four buttons.
        row = window.downloads.row("c")
        assert _pills(row) == ["WAITING"]
        assert row.now_btn.isVisibleTo(window.downloads)
        assert row.next_btn.isVisibleTo(window.downloads)
        assert row.pause_btn.isVisibleTo(window.downloads)
        assert row.cancel_btn.isVisibleTo(window.downloads)

        status = row.status_label
        assert status.text() == "Install · 1 install ahead"
        _assert_label_fits(status)

        title_label = row.title_label
        _assert_label_fits(title_label)
        if width == 1024:
            # The 4-button row is the narrowest: the long title elides, never clips.
            assert title_label.display_text().endswith("…")
            assert title_label.toolTip() == "Blue Protocol: Star Resonance"
        else:
            assert title_label.display_text() == "Blue Protocol: Star Resonance"
            assert title_label.toolTip() == ""

        for profile in window.downloads.row_profiles():
            _assert_inside_viewport(window, window.downloads.row(profile))
    finally:
        _close(window, qapp)

