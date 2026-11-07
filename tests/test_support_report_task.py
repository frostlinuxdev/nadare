# SPDX-License-Identifier: GPL-3.0-or-later
"""A failure while building the support report is shown in the dialog, never fatal."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.support_report import ReportTask


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_report_that_cannot_be_built_says_so_instead_of_raising(qapp, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("nadare.core.report.build_report", broken)
    task = ReportTask({}, "any")
    seen: list[str] = []
    task.signals.finished.connect(seen.append)
    task.run()  # an exception here would abort the whole app from the thread pool
    assert len(seen) == 1
    assert "could not be built" in seen[0] and "disk on fire" in seen[0]


def test_a_working_report_is_still_delivered_as_is(qapp, monkeypatch):
    monkeypatch.setattr("nadare.core.report.build_report", lambda *a, **k: "THE REPORT")
    monkeypatch.setattr("nadare.app._doctor_text", lambda config: "doctor")
    task = ReportTask({}, "any")
    seen: list[str] = []
    task.signals.finished.connect(seen.append)
    task.run()
    assert seen == ["THE REPORT"]
