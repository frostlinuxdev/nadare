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
