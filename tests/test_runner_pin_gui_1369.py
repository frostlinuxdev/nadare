# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""#1369: the new recommended runner is announced once, and a critical one only offers.

The announcement rides the existing runner-prefetch path: the background fetch of a bumped
pin lands the line in the window. Nothing here changes a profile or a running game.
"""
from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    qapp,
)

from nadare.core import runners
from nadare.core.events import Cancel
from nadare.core.runnerindex import Change

OLD = "dwproton-11.0-17"
NEW = "dwproton-11.0-19"


class _Config:
    def __init__(self, profiles=None) -> None:
        self.data = {"check_updates": True}
        self.profiles = profiles or {}
        self.notice = None


class _Signal:
    def __init__(self) -> None:
        self.sent = []

    def emit(self, value: object) -> None:
        self.sent.append(value)


class _Window:
    from nadare.gui.main_window_parts.cleanup import CleanupMixin as _Mixin

    _run_runner_prefetch = _Mixin._run_runner_prefetch
    _announce_pin_change = _Mixin._announce_pin_change

    def __init__(self, config) -> None:
        self.config = config
        self.runner_pin_updated = _Signal()
        self._runner_prefetch_cancel = Cancel()
        self.measured = []

    def _measure_stale(self, profiles) -> None:
        self.measured.append(profiles)


def test_prefetch_announcement_reaches_the_window(monkeypatch):
    change = Change("dwproton", OLD, NEW, critical=False)

    def fake_prefetch(profiles, *, on_change=None, **_kw):
        on_change([change])
        return []

    monkeypatch.setattr(runners, "prefetch", fake_prefetch)
    window = _Window(_Config({"g": {"backend": "wine"}}))
    window._run_runner_prefetch({"g": {"backend": "wine"}})
    assert window.runner_pin_updated.sent == [[change]]
    assert window.measured == []  # nothing was downloaded, so no cleanup offer


def test_prefetch_without_a_change_announces_nothing(monkeypatch):
    def fake_prefetch(profiles, *, on_change=None, **_kw):
        on_change([])
        return []

    monkeypatch.setattr(runners, "prefetch", fake_prefetch)
    window = _Window(_Config())
    window._run_runner_prefetch({})
    assert window.runner_pin_updated.sent == []


def test_the_line_names_the_new_pin_and_a_critical_one_counts_the_games():
    from nadare.gui.main_window_parts.cleanup import runner_pin_text

    plain = Change("dwproton", OLD, NEW, critical=False)
    assert runner_pin_text(plain, {}) == f"Recommended runner updated to {NEW}."

    critical = Change("dwproton", OLD, NEW, critical=True)
    assert runner_pin_text(critical, {"g": {"backend": "wine"}}) == (
        f"Recommended runner updated to {NEW}."
    ), "no game is on the previous build, so nothing is offered"
    profiles = {
        "on-pin": {"backend": "wine"},
        "on-previous": {"backend": "wine", "runner": f"/x/{OLD}-x86_64"},
        "own-build": {"backend": "wine", "runner": "/x/dwproton-11.0-18-x86_64"},
        "own-wine": {"backend": "wine", "wine_binary": "/usr/bin/wine"},
    }
    assert runner_pin_text(critical, profiles) == (
        f"Recommended runner updated to {NEW}. Critical fix: 1 game on {OLD} can switch "
        "in Settings."
    )
    profiles["second"] = {"backend": "wine", "runner": f"/x/{OLD}-x86_64"}
    assert "2 games on " in runner_pin_text(critical, profiles)


def test_the_window_shows_the_line_in_a_banner(tmp_path, qapp):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(_empty_config(tmp_path))
    try:
        window.show()
        qapp.processEvents()
        assert not window.runner_banner.isVisible()
        window._show_runner_pin_updated([Change("dwproton", OLD, NEW, critical=False)])
        qapp.processEvents()
        assert window.runner_banner.isVisible()
        assert window.runner_banner.message_label.text() == (
            f"Recommended runner updated to {NEW}."
        )
        assert window.runner_banner.action_button.text() == "Settings"
        window._show_runner_pin_updated([])
        assert window.runner_banner.message_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_window_is_wired_to_the_signal_and_the_banner(tmp_path, qapp):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(_empty_config(tmp_path))
    try:
        window.show()
        qapp.processEvents()
        change = Change("dwproton", OLD, NEW, critical=False)
        window.runner_pin_updated.emit([change])
        qapp.processEvents()
        assert window.runner_banner.isVisible()
        assert NEW in window.runner_banner.message_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_action_opens_settings(tmp_path, qapp, monkeypatch):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(_empty_config(tmp_path))
    try:
        calls = []
        monkeypatch.setattr(window, "open_settings", lambda: calls.append(1))
        window._show_runner_pin_updated([Change("dwproton", OLD, NEW, critical=False)])
        window.runner_banner.action_button.click()
        assert calls == [1]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_window_without_games_still_shows_the_line(tmp_path, qapp):
    """The pin can change on a machine whose games are not in the config (hand-edited games)."""
    from nadare.gui.main_window import MainWindow

    window = MainWindow(_empty_config(tmp_path))
    try:
        window.runner_pin_updated.emit([Change("dwproton", OLD, NEW, critical=True)])
        qapp.processEvents()
        assert NEW in window.runner_banner.message_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_announcement_is_sent_before_download_and_survives_failure(monkeypatch):
    change = Change("dwproton", OLD, NEW, critical=False)
    window = _Window(_Config())

    def fake_prefetch(profiles, *, on_change=None, **_kw):
        on_change([change])
        assert window.runner_pin_updated.sent == [[change]], "announce before downloading"
        raise OSError("download failed after accepting the pin")

    monkeypatch.setattr(runners, "prefetch", fake_prefetch)
    window._run_runner_prefetch({})
    assert window.runner_pin_updated.sent == [[change]]


def test_every_changed_family_is_visible(tmp_path, qapp):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(_empty_config(tmp_path))
    try:
        window._show_runner_pin_updated([
            Change("dwproton", OLD, NEW, critical=False),
            Change("ge", "GE-Proton11-7", "GE-Proton11-8", critical=False),
        ])
        text = window.runner_banner.message_label.text()
        assert NEW in text and "GE-Proton11-8" in text
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_startup_refreshes_recommendations_before_the_first_game(monkeypatch):
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    calls = []

    class Thread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("nadare.gui.main_window_parts.cleanup.threading.Thread", Thread)
    monkeypatch.setattr(
        runners, "prefetch", lambda profiles, **kwargs: calls.append(profiles) or []
    )
    window = _Window(_Config())
    CleanupMixin._start_runner_prefetch(window)
    assert calls == [{}]
