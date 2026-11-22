# SPDX-License-Identifier: GPL-3.0-or-later
"""The GUI consumes every verify shape and recovers from "not installed" (#1459).

A typed :class:`VerifyResult` is as valid as a legacy dict, and only ``failed`` is kept
as a stored file-check failure. ``not_installed`` and ``nothing_to_verify`` must not turn
a later status probe into a sticky ERROR/RETRY (the CLI already reports them that way).
"""

from __future__ import annotations

import pytest

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.verification import VerifyResult
from nadare.gui.main_window import MainWindow
from nadare.gui.main_window_parts import operations


@pytest.fixture
def window(tmp_path, qtbot):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("box", backend="wine", title="Box")
    win = MainWindow(config)
    qtbot.addWidget(win)
    win._start = lambda *a, **k: True  # the re-probe is not what these tests check
    win._current_profile = "box"
    win.game.set_profile("box", config.profile("box"))
    yield win
    win.controller._workers.clear()
    win.close()


def test_a_typed_passing_verify_result_shows_its_message(window, monkeypatch):
    infos: list[str] = []
    monkeypatch.setattr(
        operations.QMessageBox, "information", lambda *a: infos.append(a[-1])
    )
    window._on_result("box", "verify", VerifyResult("passed", "all checks passed"))
    assert infos == ["all checks passed"]
    assert "box" not in window._verify_failures


def test_a_typed_failed_verify_result_is_kept_like_a_dict(window, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(operations.QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    window._on_result("box", "verify", VerifyResult("failed", "integrity check failed"))
    assert warnings == ["integrity check failed"]
    assert window._verify_failures["box"] == "integrity check failed"
    assert window._snapshots["box"]["status"].state == "error"


@pytest.mark.parametrize(
    "report",
    [
        VerifyResult("not_installed", "The game files are gone"),
        {"outcome": "not_installed", "message": "The game files are gone"},
    ],
    ids=["typed", "dict"],
)
def test_verify_not_installed_is_not_stored_and_leads_to_install(
    window, monkeypatch, report
):
    warnings: list[str] = []
    monkeypatch.setattr(operations.QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    window._on_result("box", "status", Status("not_installed", message="The game files are gone"))
    window._verify_failures["box"] = "an older file-check failure"

    window._on_result("box", "verify", report)
    assert "box" not in window._verify_failures
    assert warnings == []

    # The re-probe after verify must show the not-installed page, not RETRY.
    window._on_result("box", "status", Status("not_installed", message="The game files are gone"))
    assert window.game._primary_op() == "install"
    assert window.game.primary.text() == "INSTALL"


def test_nothing_to_verify_shows_its_info_and_stores_no_failure(window, monkeypatch):
    infos: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        operations.QMessageBox, "information", lambda *a: infos.append(a[-1])
    )
    monkeypatch.setattr(operations.QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    window._verify_failures["box"] = "an older file-check failure"

    window._on_result(
        "box", "verify", {"outcome": "nothing_to_verify", "message": "nothing to verify"}
    )

    assert infos == ["nothing to verify"]
    assert warnings == []
    assert "box" not in window._verify_failures
