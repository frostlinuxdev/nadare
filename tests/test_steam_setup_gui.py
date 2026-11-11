# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Steam Set up dialog and the game page's Set up button."""

from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import steamsetup
from nadare.core.steamrun import ActionResult
from nadare.gui.game_view import GameView
from nadare.gui.steam_setup import SteamSetupDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _item(
    key: str,
    label: str,
    state: str,
    *,
    detail: str = "",
    action: str = "",
    warning: str = "",
    action_label: str = "",
) -> steamsetup.SetupItem:
    return steamsetup.SetupItem(
        key, label, state, detail, action, warning=warning, action_label=action_label
    )


def _plan(items, *, verified: bool = True, note: str = "") -> steamsetup.SetupPlan:
    return steamsetup.SetupPlan("5019180", "Epic Seven", verified, note, tuple(items))


def _dialog(plan_fn, act_fn=None, **kwargs) -> SteamSetupDialog:
    return SteamSetupDialog(
        None,
        plan_fn=plan_fn,
        act_fn=act_fn or (lambda item: ActionResult(True, "done")),
        **kwargs,
    )


def _close(dialog, qapp) -> None:
    dialog.close()
    dialog.deleteLater()
    qapp.processEvents()


def test_the_dialog_lists_the_plan_with_markers_details_and_the_next_step(qapp):
    plan = _plan(
        [
            _item("installed", "Installed in Steam", steamsetup.STATE_DONE),
            _item(
                "h264",
                "H.264 video unlocked",
                steamsetup.STATE_TODO,
                detail="Lets Steam play the opening video.",
                action="unlock_h264",
            ),
            _item(
                "prefix:webview2",
                "Microsoft WebView2",
                steamsetup.STATE_BLOCKED,
                detail="Install the game in Steam first.",
            ),
            _item(
                "runner",
                "DW-Proton runner",
                steamsetup.STATE_OPTIONAL,
                detail="Optional only.",
                action="set_runner",
                action_label="Use DW-Proton",
            ),
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.windowTitle() == "Set up Epic Seven"
        assert dialog.title_label.text() == "Set up Epic Seven"
        assert set(dialog.rows) == {"installed", "h264", "prefix:webview2", "runner"}
        markers = {key: row.marker_label.text() for key, row in dialog.rows.items()}
        assert markers == {
            "installed": "✓",
            "h264": "○",
            "prefix:webview2": "–",
            "runner": "+",
        }
        assert dialog.rows["h264"].text_label.text() == "H.264 video unlocked"
        assert dialog.rows["prefix:webview2"].detail_label.text() == (
            "Install the game in Steam first."
        )
        assert dialog.rows["prefix:webview2"].detail_label.objectName() == "Dim"
        # Only an offered (optional/recommended) item carries its own button.
        assert not hasattr(dialog.rows["h264"], "button")
        assert dialog.rows["runner"].button.text() == "Use DW-Proton"
        assert "H.264 video unlocked" in dialog.next_button.text()  # no action_label: its name
        assert dialog.next_button.isEnabled()
    finally:
        _close(dialog, qapp)


def test_recommended_and_optional_markers(qapp):
    plan = _plan(
        [
            _item("runner", "DW-Proton runner", steamsetup.STATE_RECOMMENDED, action="set_runner")
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.rows["runner"].marker_label.text() == "▲"
    finally:
        _close(dialog, qapp)


def test_the_note_is_shown_for_an_unverified_game_or_a_note(qapp):
    unverified = _plan([], verified=False, note="not tested yet")
    dialog = _dialog(lambda: unverified)
    try:
        assert not dialog.note_label.isHidden()
        assert "not tested yet" in dialog.note_label.text()
    finally:
        _close(dialog, qapp)

    dialog = _dialog(lambda: _plan([], note="a note"))
    try:
        assert not dialog.note_label.isHidden()
        assert dialog.note_label.text() == "a note"
    finally:
        _close(dialog, qapp)

    dialog = _dialog(lambda: _plan([]))
    try:
        assert dialog.note_label.isHidden()
    finally:
        _close(dialog, qapp)


def test_the_next_step_runs_off_the_gui_thread_and_shows_the_result(qapp):
    plan_calls: list[int] = []
    act_threads: list[threading.Thread] = []
    confirms: list[tuple[str, str]] = []
    plan = _plan(
        [
            _item(
                "h264",
                "H.264 video unlocked",
                steamsetup.STATE_TODO,
                action="unlock_h264",
                warning="Unlock H.264?",
            )
        ]
    )

    def plan_fn():
        plan_calls.append(1)
        return plan

    def act_fn(item):
        act_threads.append(threading.current_thread())
        return ActionResult(True, "Steam's H.264 video support was requested.")

    def confirm_fn(title, text):
        confirms.append((title, text))
        return True

    dialog = _dialog(plan_fn, act_fn, confirm_fn=confirm_fn)
    try:
        dialog.next_button.click()
        assert _wait(qapp, lambda: "H.264 video support" in dialog.result_label.text())
        assert confirms == [("Set up Epic Seven", "Unlock H.264?")]
        assert act_threads and act_threads[0] is not threading.main_thread()
        assert len(plan_calls) >= 2  # once at start, once after the action
        assert not dialog.busy
        assert dialog.result_label.property("state") in ("", None)
    finally:
        _close(dialog, qapp)


def test_a_declined_confirmation_changes_nothing(qapp):
    calls: list[steamsetup.SetupItem] = []
    plan = _plan(
        [
            _item(
                "h264",
                "H.264 video unlocked",
                steamsetup.STATE_TODO,
                action="unlock_h264",
                warning="Unlock H.264?",
            )
        ]
    )
    dialog = _dialog(
        lambda: plan,
        calls.append,
        confirm_fn=lambda title, text: False,
    )
    try:
        before = dialog.result_label.text()
        dialog.next_button.click()
        assert calls == []
        assert dialog.result_label.text() == before
        assert not dialog.busy
    finally:
        _close(dialog, qapp)


def test_an_action_that_raises_is_a_bad_result_instead_of_a_crash(qapp):
    def act_fn(item):
        raise RuntimeError("steam is gone")

    dialog = _dialog(
        lambda: _plan(
            [_item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")]
        ),
        act_fn,
    )
    try:
        dialog.next_button.click()
        assert _wait(qapp, lambda: "steam is gone" in dialog.result_label.text())
        assert dialog.result_label.property("state") == "bad"
        assert not dialog.busy
    finally:
        _close(dialog, qapp)


def test_busy_disables_every_button_and_says_working(qapp):
    started = threading.Event()
    release = threading.Event()

    def act_fn(item):
        started.set()
        release.wait(5.0)
        return ActionResult(True, "finished")

    plan = _plan(
        [
            _item("installed", "Installed in Steam", steamsetup.STATE_DONE),
            _item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner"),
        ]
    )
    dialog = _dialog(lambda: plan, act_fn)
    try:
        dialog.rows["runner"].button.click()
        assert _wait(qapp, started.is_set)
        assert dialog.busy
        assert dialog.result_label.text() == "Working…"
        assert not dialog.rows["runner"].button.isEnabled()
        assert not dialog.next_button.isEnabled()
        assert not dialog.undo_button.isEnabled()
        assert not dialog.close_button.isEnabled()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
    finally:
        release.set()
        _close(dialog, qapp)


def test_a_runner_change_offers_undo_and_undo_calls_undo_fn(qapp):
    previous = object()
    undo_args: list[object] = []
    plan = _plan(
        [_item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner")]
    )

    def act_fn(item):
        return ActionResult(True, "Steam now uses DW-Proton.", changed=True, previous=previous)

    def undo_fn(arg):
        undo_args.append(arg)
        return ActionResult(True, "The earlier runner is back.")

    dialog = _dialog(lambda: plan, act_fn, undo_fn=undo_fn)
    try:
        assert dialog.undo_button.isHidden()
        dialog.rows["runner"].button.click()
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        dialog.undo_button.click()
        assert _wait(qapp, lambda: undo_args == [previous])
        assert _wait(qapp, lambda: "earlier runner" in dialog.result_label.text())
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)


def test_the_dialog_survives_being_closed_while_an_action_runs(qapp):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def act_fn(item):
        started.set()
        release.wait(5.0)
        finished.set()
        return ActionResult(True, "done")

    dialog = _dialog(
        lambda: _plan(
            [_item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")]
        ),
        act_fn,
    )
    dialog.next_button.click()
    assert _wait(qapp, started.is_set)
    dialog.close()
    dialog.deleteLater()
    qapp.processEvents()
    release.set()
    assert _wait(qapp, finished.is_set)
    QThreadPool.globalInstance().waitForDone(2000)


def test_the_game_page_shows_set_up_only_for_a_known_steam_game(qapp):
    view = GameView()
    try:
        view.set_profile("p", {"backend": "steam", "steam_app_id": "5019180"})
        assert not view.steam_setup_btn.isHidden()
        assert view.steam_setup_btn.text() == "Set up"
        assert "Checks this Steam game" in view.steam_setup_btn.toolTip()

        emitted: list[bool] = []
        view.steam_setup_requested.connect(lambda: emitted.append(True))
        view.steam_setup_btn.click()
        assert emitted == [True]

        view.set_profile("p", {"backend": "steam", "steam_app_id": "999"})
        assert view.steam_setup_btn.isHidden()

        view.set_profile("p", {"backend": "direct-pgr", "app_id": "5019180"})
        assert view.steam_setup_btn.isHidden()
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_the_game_page_accepts_app_id_and_extra_and_hides_while_busy(qapp):
    view = GameView()
    try:
        view.set_profile("p", {"backend": "steam", "app_id": "3224770"})
        assert not view.steam_setup_btn.isHidden()

        view.set_profile("p", {"backend": "steam", "extra": {"steam_app_id": "3557620"}})
        assert not view.steam_setup_btn.isHidden()

        view.set_profile("p", {"backend": "steam", "steam_app_id": "5019180"})
        view.set_busy("install")
        assert view.steam_setup_btn.isHidden()
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def teardown_module(module):
    QThreadPool.globalInstance().waitForDone(2000)


def test_a_second_start_while_a_step_runs_is_ignored(qapp):
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def act_fn(item):
        calls.append(item.key)
        started.set()
        release.wait(5.0)
        return ActionResult(True, "done")

    item = _item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")
    dialog = _dialog(lambda: _plan([item]), act_fn)
    try:
        dialog._run_item(item)
        assert _wait(qapp, started.is_set)
        dialog._run_item(item)  # the user is fast: a second request while busy
        dialog._run_next()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        assert calls == ["h264"]
    finally:
        release.set()
        _close(dialog, qapp)


def test_a_result_for_a_closed_dialog_raises_nothing_in_the_worker(qapp):
    from PyQt6 import sip

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    seen: list[object] = []
    old_hook = sys.excepthook
    sys.excepthook = lambda *args: seen.append(args)

    def act_fn(item):
        started.set()
        release.wait(5.0)
        finished.set()
        return ActionResult(True, "done")

    dialog = _dialog(
        lambda: _plan(
            [_item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")]
        ),
        act_fn,
    )
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        sip.delete(dialog._signals)  # the receiver is really gone, not just hidden
        release.set()
        assert _wait(qapp, finished.is_set)
        QThreadPool.globalInstance().waitForDone(3000)
        qapp.processEvents()
        assert seen == []
    finally:
        release.set()
        sys.excepthook = old_hook
        dialog.deleteLater()
        qapp.processEvents()


def test_the_buttons_say_what_pressing_them_does(qapp):
    plan = _plan(
        [
            _item("installed", "Installed in Steam", steamsetup.STATE_DONE),
            _item(
                "h264", "H.264 video unlocked", steamsetup.STATE_TODO,
                action="unlock_h264", action_label="Unlock H.264 video",
            ),
            _item(
                "runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL,
                action="set_runner", action_label="Use DW-Proton",
            ),
            _item(
                "prefix:webview2", "Microsoft WebView2", steamsetup.STATE_OPTIONAL,
                action="install_prefix:webview2", action_label="Install Microsoft WebView2",
            ),
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.next_button.text() == "Unlock H.264 video"
        assert dialog.rows["runner"].button.text() == "Use DW-Proton"
        assert dialog.rows["prefix:webview2"].button.text() == "Install Microsoft WebView2"
    finally:
        _close(dialog, qapp)


def test_a_step_without_an_action_label_falls_back_to_its_name(qapp):
    plan = _plan(
        [_item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.next_button.text() == "Do next step: H.264 video unlocked"
    finally:
        _close(dialog, qapp)


def test_a_game_that_had_no_earlier_runner_can_still_be_undone(qapp):
    """Seen on the real Steam: no earlier entry (previous is None) hid the Undo button."""
    undo_args: list[object] = []
    plan = _plan(
        [_item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner")]
    )

    def act_fn(item):
        return ActionResult(True, "Steam now uses DW-Proton.", changed=True, previous=None)

    def undo_fn(arg):
        undo_args.append(arg)
        return ActionResult(True, "The setting is removed.")

    dialog = _dialog(lambda: plan, act_fn, undo_fn=undo_fn)
    try:
        dialog.rows["runner"].button.click()
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        dialog.undo_button.click()
        assert _wait(qapp, lambda: undo_args == [None])
        assert _wait(qapp, lambda: dialog.undo_button.isHidden())
    finally:
        _close(dialog, qapp)


def test_a_change_that_changed_nothing_offers_no_undo(qapp):
    plan = _plan(
        [_item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner")]
    )
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, "Already set.", changed=False, previous=None),
        undo_fn=lambda arg: ActionResult(True, "x"),
    )
    try:
        dialog.rows["runner"].button.click()
        assert _wait(qapp, lambda: "Already set" in dialog.result_label.text())
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)


def test_only_the_runner_step_offers_an_undo(qapp):
    """Seen on the real Steam: a WebView2 install (changed, no previous) offered 'Undo runner'."""
    plan = _plan(
        [
            _item(
                "prefix:webview2", "Microsoft WebView2", steamsetup.STATE_TODO,
                action="install_prefix:webview2",
            ),
            _item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264"),
        ]
    )
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, "done", changed=True, previous=None),
        undo_fn=lambda arg: ActionResult(True, "x"),
    )
    try:
        dialog.next_button.click()
        assert _wait(qapp, lambda: not dialog.busy and dialog.result_label.text() == "done")
        assert dialog.undo_button.isHidden()
        dialog._run_item(plan.items[1])
        assert _wait(qapp, lambda: not dialog.busy)
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)


# ---- scrutiny fixes -----------------------------------------------------------------------
def _runner_plan():
    return _plan(
        [
            _item(
                "runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL,
                action="set_runner", action_label="Use DW-Proton",
            ),
            _item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264"),
        ]
    )


def test_another_step_does_not_take_the_runner_undo_away(qapp):
    plan = _runner_plan()
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, item.key, changed=True, previous=None),
        undo_fn=lambda arg: ActionResult(True, "x"),
    )
    try:
        dialog._run_item(plan.items[0])
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        dialog._run_item(plan.items[1])
        assert _wait(qapp, lambda: dialog.result_label.text() == "h264")
        assert not dialog.undo_button.isHidden()  # still the last runner change
    finally:
        _close(dialog, qapp)


def test_a_failed_undo_can_be_tried_again(qapp):
    plan = _runner_plan()
    answers = iter([ActionResult(False, "Steam did not close."), ActionResult(True, "back")])
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, "set", changed=True, previous=None),
        undo_fn=lambda arg: next(answers),
    )
    try:
        dialog._run_item(plan.items[0])
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        dialog.undo_button.click()
        assert _wait(qapp, lambda: "did not close" in dialog.result_label.text())
        assert not dialog.undo_button.isHidden() and dialog.undo_button.isEnabled()
        dialog.undo_button.click()
        assert _wait(qapp, lambda: dialog.undo_button.isHidden())
        assert dialog.result_label.text() == "back"
    finally:
        _close(dialog, qapp)


def test_a_failed_refresh_does_not_hide_a_successful_step_or_lock_the_dialog(qapp):
    calls = {"n": 0}
    plan = _runner_plan()

    def plan_fn():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("config.vdf is unreadable")
        return plan

    dialog = _dialog(plan_fn, lambda item: ActionResult(True, "Installed it."))
    try:
        dialog.next_button.click()
        assert _wait(qapp, lambda: not dialog.busy)
        text = dialog.result_label.text()
        assert "Installed it." in text and "could not be refreshed" in text
        assert dialog.close_button.isEnabled() and dialog.next_button.isEnabled()
    finally:
        _close(dialog, qapp)


def test_a_step_that_returns_nothing_is_shown_as_a_failure(qapp):
    plan = _runner_plan()
    dialog = _dialog(lambda: plan, lambda item: None)
    try:
        dialog.next_button.click()
        assert _wait(qapp, lambda: "returned no result" in dialog.result_label.text())
    finally:
        _close(dialog, qapp)


def test_escape_and_the_close_button_do_nothing_while_a_step_runs(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    started, release = threading.Event(), threading.Event()

    def act_fn(item):
        started.set()
        release.wait(5.0)
        return ActionResult(True, "done")

    plan = _runner_plan()
    dialog = _dialog(lambda: plan, act_fn)
    dialog.show()
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.isVisible()
        assert dialog.close() is False
        assert dialog.isVisible()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()  # idle again: Escape closes as usual
    finally:
        release.set()
        _close(dialog, qapp)


def test_the_running_step_counter_covers_the_whole_step_even_if_the_window_is_gone(qapp):
    from PyQt6 import sip

    from nadare.gui import steam_setup

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    assert steam_setup.steps_running() == 0

    def act_fn(item):
        started.set()
        release.wait(5.0)
        finished.set()
        return ActionResult(True, "done")

    plan = _runner_plan()
    dialog = _dialog(lambda: plan, act_fn)
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        assert steam_setup.steps_running() == 1
        sip.delete(dialog._signals)
        release.set()
        assert _wait(qapp, finished.is_set)
        QThreadPool.globalInstance().waitForDone(3000)
        assert steam_setup.steps_running() == 0
    finally:
        release.set()
        dialog.deleteLater()
        qapp.processEvents()


def test_the_plan_is_computed_off_the_gui_thread_when_asked(qapp):
    main = threading.get_ident()
    seen: list[int] = []
    gate = threading.Event()
    plan = _runner_plan()

    def plan_fn():
        seen.append(threading.get_ident())
        gate.wait(5.0)
        return plan

    dialog = _dialog(plan_fn, async_plan=True)
    try:
        assert dialog.rows == {}  # nothing to show yet
        assert "Checking" in dialog.result_label.text()
        assert not dialog.next_button.isEnabled()
        assert dialog.close_button.isEnabled()  # nothing has started yet: leaving is fine
        gate.set()
        assert _wait(qapp, lambda: set(dialog.rows) == {"runner", "h264"})
        assert seen and all(tid != main for tid in seen)
        assert dialog.result_label.text() == ""
        assert dialog.next_button.isEnabled()
    finally:
        gate.set()
        _close(dialog, qapp)


def test_a_plan_that_fails_is_shown_and_the_window_can_still_be_closed(qapp):
    def plan_fn():
        raise RuntimeError("Steam was not found")

    dialog = _dialog(plan_fn, async_plan=True)
    try:
        assert _wait(qapp, lambda: "Steam was not found" in dialog.result_label.text())
        assert dialog.close_button.isEnabled() and not dialog.next_button.isEnabled()
    finally:
        _close(dialog, qapp)


def test_the_plan_after_a_step_is_also_computed_off_the_gui_thread(qapp):
    main = threading.get_ident()
    seen: list[int] = []
    plan = _runner_plan()

    def plan_fn():
        seen.append(threading.get_ident())
        return plan

    dialog = _dialog(plan_fn, lambda item: ActionResult(True, "ok"), async_plan=True)
    try:
        assert _wait(qapp, lambda: bool(dialog.rows))
        before = len(seen)
        dialog.next_button.click()
        assert _wait(qapp, lambda: not dialog.busy and dialog.result_label.text() == "ok")
        assert len(seen) > before and all(tid != main for tid in seen)
    finally:
        _close(dialog, qapp)


def test_a_finished_step_redraws_the_checklist_from_the_new_plan(qapp):
    todo = _item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")
    before = _plan([todo])
    after = _plan([_item("h264", "H.264 video unlocked", steamsetup.STATE_DONE)])
    plans = iter([before, after])
    dialog = _dialog(lambda: next(plans), lambda item: ActionResult(True, "ok"))
    try:
        assert dialog.rows["h264"].marker_label.text() == "○"
        dialog.next_button.click()
        assert _wait(qapp, lambda: not dialog.busy and dialog.result_label.text() == "ok")
        assert dialog.rows["h264"].marker_label.text() == "✓"
        assert not dialog.next_button.isEnabled()  # nothing left to do
    finally:
        _close(dialog, qapp)


def test_the_game_page_hides_set_up_while_the_game_runs(qapp):
    from nadare.core.backends import Status

    view = GameView()
    try:
        view.set_profile("p", {"backend": "steam", "steam_app_id": "5019180"})
        view.set_status(Status(state="installed"))
        assert not view.steam_setup_btn.isHidden()
        view.set_status(Status(state="installed", running=True))
        assert view.steam_setup_btn.isHidden()  # the runner step would close Steam under it
        view.set_status(Status(state="installed"))
        assert not view.steam_setup_btn.isHidden()
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_an_unverified_game_is_labelled_as_such_whatever_its_own_note_says(qapp):
    dialog = _dialog(lambda: _plan([], verified=False, note="the region is locked"))
    try:
        text = dialog.note_label.text()
        assert "Not verified on a real Steam yet." in text
        assert "the region is locked" in text
    finally:
        _close(dialog, qapp)
    dialog = _dialog(lambda: _plan([], verified=True, note="a note"))
    try:
        assert "Not verified" not in dialog.note_label.text()
    finally:
        _close(dialog, qapp)


def test_the_wayland_step_offers_its_own_undo(qapp):
    previous = object()
    undo_args: list[object] = []
    plan = _plan(
        [_item("wayland", "Native Wayland", steamsetup.STATE_OPTIONAL, action="set_wayland")]
    )
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, "Native Wayland is on.", changed=True, previous=previous),
        undo_fn=lambda arg: undo_args.append(arg) or ActionResult(True, "The options are back."),
    )
    try:
        assert dialog.undo_button.isHidden()
        dialog.rows["wayland"].button.click()
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        assert dialog.undo_button.text() == "Undo native Wayland"
        dialog.undo_button.click()
        assert _wait(qapp, lambda: undo_args == [previous])
        assert _wait(qapp, lambda: dialog.undo_button.isHidden())
    finally:
        _close(dialog, qapp)


def test_each_undo_is_named_for_the_step_it_undoes(qapp):
    plan = _plan(
        [_item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner")]
    )
    dialog = _dialog(
        lambda: plan,
        lambda item: ActionResult(True, "ok", changed=True, previous=None),
        undo_fn=lambda arg: ActionResult(True, "x"),
    )
    try:
        dialog.rows["runner"].button.click()
        assert _wait(qapp, lambda: not dialog.undo_button.isHidden())
        assert dialog.undo_button.text() == "Undo runner change"
    finally:
        _close(dialog, qapp)


# ---- an undo kept from an earlier session -------------------------------------------------
def _done_plan(key: str, label: str):
    return _plan([_item(key, label, steamsetup.STATE_DONE)])


@pytest.mark.parametrize(
    ("action", "key", "text"),
    [
        ("set_runner", "runner", "Undo runner change"),
        ("set_wayland", "wayland", "Undo native Wayland"),
    ],
)
@pytest.mark.parametrize("async_plan", [False, True])
def test_an_undo_from_an_earlier_session_is_offered_while_the_change_is_still_there(
    qapp, action, key, text, async_plan
):
    previous = object()
    seen: list[object] = []
    dialog = _dialog(
        lambda: _done_plan(key, "x"),
        undo_fn=lambda arg: seen.append(arg) or ActionResult(True, "back"),
        initial_undo=(action, previous),
        async_plan=async_plan,
    )
    try:
        assert _wait(qapp, lambda: not dialog.busy)
        assert not dialog.undo_button.isHidden() and dialog.undo_button.text() == text
        dialog.undo_button.click()
        assert _wait(qapp, lambda: seen == [previous])
        assert _wait(qapp, lambda: dialog.undo_button.isHidden())
    finally:
        _close(dialog, qapp)


def test_after_an_undo_the_change_before_it_can_be_undone_too(qapp):
    """#1031: Wayland after the runner used to lose the runner's undo for good."""
    plan = _plan([
        _item("runner", "DW-Proton runner", steamsetup.STATE_DONE),
        _item("wayland", "Native Wayland", steamsetup.STATE_DONE),
    ])
    runner_previous, wayland_previous = object(), object()
    stack = [("set_runner", runner_previous)]
    seen: list[object] = []
    dialog = _dialog(
        lambda: plan,
        undo_fn=lambda arg: seen.append(arg) or ActionResult(True, "back"),
        initial_undo=("set_wayland", wayland_previous),
        next_undo_fn=lambda: stack.pop() if stack else None,
    )
    try:
        assert dialog.undo_button.text() == "Undo native Wayland"
        dialog.undo_button.click()
        assert _wait(qapp, lambda: seen == [wayland_previous])
        assert _wait(qapp, lambda: dialog.undo_button.text() == "Undo runner change")
        assert not dialog.undo_button.isHidden()
        dialog.undo_button.click()
        assert _wait(qapp, lambda: seen == [wayland_previous, runner_previous])
        assert _wait(qapp, lambda: dialog.undo_button.isHidden())
    finally:
        _close(dialog, qapp)


def test_an_old_undo_is_not_offered_once_the_setting_is_no_longer_ours(qapp):
    # Steam's setting was changed by hand since: the step is not done any more
    plan = _plan([_item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL)])
    dialog = _dialog(
        lambda: plan, undo_fn=lambda arg: ActionResult(True, "back"),
        initial_undo=("set_runner", None),
    )
    try:
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)


def test_an_old_undo_with_no_undo_function_or_an_unknown_step_is_ignored(qapp):
    plan = _done_plan("runner", "x")
    dialog = _dialog(lambda: plan, initial_undo=("set_runner", None))  # no undo_fn
    try:
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)
    dialog = _dialog(
        lambda: plan, undo_fn=lambda arg: ActionResult(True, "x"),
        initial_undo=("unlock_h264", None),
    )
    try:
        assert dialog.undo_button.isHidden()
    finally:
        _close(dialog, qapp)


# ---- Cancel for a step that is running ----------------------------------------------------
def _slow_dialog(qapp, action: str, *, cancel_fn, result=None):
    started, release = threading.Event(), threading.Event()
    plan = _plan([_item("x", "A step", steamsetup.STATE_TODO, action=action)])

    def act_fn(item):
        started.set()
        release.wait(5)
        return result or ActionResult(False, "Cancelled. Nothing was changed.")

    dialog = _dialog(lambda: plan, act_fn, cancel_fn=cancel_fn)
    return dialog, started, release


@pytest.mark.parametrize("action", ["set_runner", "install_prefix:webview2"])
def test_a_running_download_step_can_be_cancelled(qapp, action):
    calls: list[int] = []
    dialog, started, release = _slow_dialog(qapp, action, cancel_fn=lambda: calls.append(1))
    try:
        assert dialog.cancel_button.isHidden()  # nothing runs: nothing to cancel
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        assert not dialog.cancel_button.isHidden() and dialog.cancel_button.isEnabled()
        dialog.cancel_button.click()
        dialog.cancel_button.click()  # a second click does nothing more
        assert calls == [1]
        assert not dialog.cancel_button.isEnabled()
        assert "Cancelling" in dialog.cancel_button.text()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        assert dialog.cancel_button.isHidden()
        assert dialog.cancel_button.text() == "Cancel"  # ready for the next step
    finally:
        release.set()
        _close(dialog, qapp)


@pytest.mark.parametrize("action", ["steam_install", "unlock_h264", "set_wayland"])
def test_a_step_with_nothing_to_stop_has_no_cancel_button(qapp, action):
    dialog, started, release = _slow_dialog(qapp, action, cancel_fn=lambda: None)
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        assert dialog.cancel_button.isHidden()  # closing Steam or one quick request: not stoppable
    finally:
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        _close(dialog, qapp)


def test_there_is_no_cancel_button_without_a_cancel_function(qapp):
    dialog, started, release = _slow_dialog(qapp, "set_runner", cancel_fn=None)
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        assert dialog.cancel_button.isHidden()
    finally:
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        _close(dialog, qapp)


def test_a_cancel_that_came_too_late_is_said_so(qapp):
    dialog, started, release = _slow_dialog(
        qapp, "set_runner", cancel_fn=lambda: None,
        result=ActionResult(True, "Steam now uses DW-Proton.", changed=True),
    )
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        dialog.cancel_button.click()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        text = dialog.result_label.text()
        assert "Steam now uses DW-Proton." in text and "too late" in text
    finally:
        release.set()
        _close(dialog, qapp)


def test_a_cancelled_step_does_not_say_too_late(qapp):
    dialog, started, release = _slow_dialog(qapp, "set_runner", cancel_fn=lambda: None)
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        dialog.cancel_button.click()
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        assert "too late" not in dialog.result_label.text()
        assert "Cancelled" in dialog.result_label.text()
    finally:
        release.set()
        _close(dialog, qapp)


def test_refresh_during_the_first_check_does_not_plan_on_the_gui_thread(qapp):
    """#925: reopening Set up while "Checking Steam…" ran the planner on the GUI thread."""
    main = threading.get_ident()
    seen: list[int] = []
    gate = threading.Event()
    plan = _runner_plan()

    def plan_fn():
        seen.append(threading.get_ident())
        gate.wait(5.0)
        return plan

    dialog = _dialog(plan_fn, async_plan=True)
    try:
        dialog.refresh()  # the main window does this when the same game's window is reopened
        assert main not in seen
        gate.set()
        assert _wait(qapp, lambda: set(dialog.rows) == {"runner", "h264"})
        assert main not in seen
    finally:
        gate.set()
        _close(dialog, qapp)
