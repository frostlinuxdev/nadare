# SPDX-License-Identifier: GPL-3.0-or-later
"""The in-app log viewer: lists logs, shows the latest run, searches, redacts what it copies."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.gui.logs_dialog import LogsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

H = "===== 2026-10-26T10:00:0{n} launch (G) | nadare 0.1.0b12 =====\n"


@pytest.fixture
def logs(tmp_path):
    home = os.path.expanduser("~")
    (tmp_path / "G-launch.log").write_text(
        H.format(n=1) + "old run\n" + H.format(n=2) + f"new run\nERROR boom at {home}/x\nfine\n",
        encoding="utf-8",
    )
    (tmp_path / "G-install.log").write_text("installing\n", encoding="utf-8")
    (tmp_path / "nadare.log").write_text("app line\n", encoding="utf-8")
    return tmp_path


def make(logs, profile="G", **kw):
    return LogsDialog(["G"], profile=profile, directory=logs, follow=False, **kw)


def test_lists_the_profiles_logs_and_selects_the_newest(qapp, logs):
    dlg = make(logs)
    assert dlg.files.count() >= 2
    assert "new run" in dlg.view.toPlainText() or "installing" in dlg.view.toPlainText()


def test_shows_only_the_latest_run_until_unticked(qapp, logs):
    dlg = make(logs, operation="launch")
    assert dlg.latest_only.isChecked()
    text = dlg.view.toPlainText()
    assert "new run" in text and "old run" not in text
    dlg.latest_only.setChecked(False)
    assert "old run" in dlg.view.toPlainText()


def test_search_filters_lines(qapp, logs):
    dlg = make(logs, operation="launch")
    dlg.search.setText("boom")
    assert dlg.view.toPlainText().strip().splitlines()[-1].startswith("ERROR boom")
    assert "fine" not in dlg.view.toPlainText()


def test_current_text_is_redacted_by_default(qapp, logs):
    home = os.path.expanduser("~")
    dlg = make(logs, operation="launch")
    assert home not in dlg.current_text()
    dlg.redact_box.setChecked(False)
    assert home in dlg.current_text()


def test_all_profiles_view_lists_the_app_log_too(qapp, logs):
    dlg = LogsDialog(["G"], profile=None, directory=logs, follow=False)
    labels = [dlg.files.item(i).text() for i in range(dlg.files.count())]
    assert any("Nadare" in label for label in labels)


def test_refresh_picks_up_appended_lines(qapp, logs):
    dlg = make(logs, operation="launch")
    with (logs / "G-launch.log").open("a", encoding="utf-8") as fh:
        fh.write("late line\n")
    dlg.refresh()
    assert "late line" in dlg.view.toPlainText()


def test_the_support_report_button_asks_for_the_selected_profiles_report(qapp, logs):
    dlg = make(logs, operation="launch")
    got = []
    dlg.report_requested.connect(got.append)
    dlg.report_btn.click()
    assert got == ["G"]
    assert "support report" in dlg.report_btn.text().lower()


def test_the_support_report_button_falls_back_to_the_first_profile(qapp, logs):
    dlg = LogsDialog(["G"], profile=None, directory=logs, follow=False)
    got = []
    dlg.report_requested.connect(got.append)
    dlg.report_btn.click()
    assert got == ["G"]
