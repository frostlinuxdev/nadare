# SPDX-License-Identifier: GPL-3.0-or-later
"""#1031: after "Install in Steam" the checklist can be checked again, off the GUI thread.

Steam installs the game in the background; the checklist kept saying "not installed" with no
way to look again short of closing the window. Re-planning runs Vulkan and Steam probes, so it
must not run on the GUI thread (#925), the reopen path's ``refresh()`` included.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    pass
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_steam_setup_gui import _close, _dialog, _item, _plan, _wait, qapp  # noqa: F401

from nadare.core import steamsetup


def _plans():
    state = {"installed": False, "threads": []}

    def plan_fn():
        state["threads"].append(threading.current_thread() is threading.main_thread())
        installed = steamsetup.STATE_DONE if state["installed"] else steamsetup.STATE_RECOMMENDED
        return _plan([_item("installed", "Installed in Steam", installed, action="install")])

    return state, plan_fn


def test_check_again_picks_up_a_game_steam_installed_meanwhile(qapp):  # noqa: F811
    state, plan_fn = _plans()
    dialog = _dialog(plan_fn)
    try:
        before = dialog._plan.items[0].state
        assert before == steamsetup.STATE_RECOMMENDED
        state["installed"] = True
        assert not dialog.check_button.isHidden() and dialog.check_button.isEnabled()
        dialog.check_button.click()
        assert _wait(qapp, lambda: dialog._plan.items[0].state == steamsetup.STATE_DONE)
        assert state["threads"][-1] is False  # planned on the pool, not the GUI thread
    finally:
        _close(dialog, qapp)


def test_refresh_also_plans_off_the_gui_thread_and_keeps_the_result_line(qapp):  # noqa: F811
    state, plan_fn = _plans()
    dialog = _dialog(plan_fn)
    try:
        dialog.result_label.setText("Steam installed the runner.")
        state["installed"] = True
        dialog.refresh()
        assert _wait(qapp, lambda: dialog._plan.items[0].state == steamsetup.STATE_DONE)
        assert state["threads"][-1] is False
        assert dialog.result_label.text() == "Steam installed the runner."
    finally:
        _close(dialog, qapp)


def test_check_again_is_off_while_a_step_runs(qapp):  # noqa: F811
    state, plan_fn = _plans()
    dialog = _dialog(plan_fn)
    try:
        dialog._busy = True
        dialog._apply_enabled()
        assert not dialog.check_button.isEnabled()
        calls = len(state["threads"])
        dialog.check_again()
        assert len(state["threads"]) == calls
    finally:
        dialog._busy = False
        _close(dialog, qapp)


def test_the_window_can_close_during_a_re_check(qapp):  # noqa: F811
    gate = threading.Event()

    def slow_plan():
        gate.wait(5)
        return _plan([_item("installed", "Installed in Steam", steamsetup.STATE_DONE)])

    dialog = _dialog(lambda: _plan([_item("installed", "Installed", steamsetup.STATE_DONE)]))
    try:
        dialog._plan_fn = slow_plan
        dialog.check_again()
        dialog.done(0)
        assert not dialog.isVisible()
    finally:
        gate.set()
        _close(dialog, qapp)
