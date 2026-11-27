# SPDX-License-Identifier: GPL-3.0-or-later
"""A status check that ends late must not clear the page of an install that is running (#895)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.add_profile("g", backend="wine", title="G")
    win = MainWindow(config)
    win.controller.start = lambda profile, op, payload=None: True
    win.open_game("g")
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _installing(window):
    window._busy["g"] = "install"
    window.game._busy_op = "install"


@pytest.mark.parametrize("operation", ["status", "news", "media", "check_update"])
def test_a_late_background_success_leaves_a_running_install_alone(window, operation):
    _installing(window)
    window._on_succeeded("g", operation, 0)
    assert window.game._busy_op == "install"
    assert window._busy.get("g") == "install"


def test_a_late_status_failure_leaves_a_running_install_alone(window):
    _installing(window)
    window._on_failed("g", "status", "could not probe")
    assert window.game._busy_op == "install"


def test_a_late_status_cancel_leaves_a_running_install_alone(window):
    _installing(window)
    window._on_cancelled("g", "status")
    assert window.game._busy_op == "install"


def test_the_install_itself_still_finishes_normally(window):
    _installing(window)
    window._on_succeeded("g", "install", 0)
    assert window.game._busy_op is None
    assert "g" not in window._busy


def test_a_status_check_on_an_idle_page_still_finishes_it(window):
    window.game._busy_op = "status"
    window._on_succeeded("g", "status", 0)
    assert window.game._busy_op is None


def test_a_status_failure_for_a_removed_game_does_not_bring_its_state_back(window):
    """Round 4: _on_result had a generation guard, _on_failed did not."""
    from types import SimpleNamespace

    window._start("g", "status", silent=True)  # records the worker's generation
    window._on_game_removed("g", SimpleNamespace(profile_kept=False, warnings=[]))
    assert "g" not in window.config.profiles
    window._on_failed("g", "status", "offline")
    assert "g" not in window._snapshots
    assert "g" not in window._status_errors


def _installed(window):
    from nadare.core.backends import Status

    window.game.set_status(Status("installed"))
    assert window.game._primary_op() == "launch"


def test_play_waits_for_the_status_after_an_update_check(window):
    """A Play pressed between the update check and its status refresh launched an outdated
    game (Strinova, 2026-11-27): the page waits for the refreshed status instead."""
    _installed(window)
    window._on_succeeded("g", "check_update", 0)
    assert window.game._awaiting_status
    assert window.game._primary_op() is None
    window.game.set_status(window.game._status)  # the refreshed status arrives
    assert window.game._primary_op() == "launch"


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_a_status_refresh_that_never_answers_does_not_leave_the_page_waiting(window, ending):
    _installed(window)
    window._on_succeeded("g", "check_update", 0)
    if ending == "cancelled":
        window._on_cancelled("g", "status")
    else:
        window._on_failed("g", "status", "offline")
    assert not window.game._awaiting_status
    assert window.game._primary_op() == ("launch" if ending == "cancelled" else "retry")


def test_a_queued_operation_cancelled_behind_the_refresh_frees_the_page(window):
    """The controller detaches the status worker when a queued job is cancelled, so its own
    cancel never arrives; the queued job's start ends the wait instead (Neon, 2026-11-27)."""
    _installed(window)
    window._on_succeeded("g", "check_update", 0)
    window.game.set_busy("update")
    window.game.cancelled()
    assert not window.game._awaiting_status
    assert window.game._primary_op() == "launch"


def test_the_refresh_is_not_awaited_when_it_could_not_start(window):
    _installed(window)
    window.controller.start = lambda profile, op, payload=None: False
    window._on_succeeded("g", "check_update", 0)
    assert not window.game._awaiting_status
    assert window.game._primary_op() == "launch"


def test_an_update_check_for_another_game_leaves_this_page_alone(window):
    _installed(window)
    window.config.add_profile("h", backend="wine", title="H")
    window._on_succeeded("h", "check_update", 0)
    assert not window.game._awaiting_status
