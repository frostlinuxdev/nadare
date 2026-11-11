# SPDX-License-Identifier: GPL-3.0-or-later
"""Set up's runner step shows the runner download's progress, not minutes of "Working…"."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading

import pytest

from nadare.core import runners, steamglue, steamsetup
from nadare.core.events import Event


def test_the_runner_step_passes_progress_on(tmp_path, monkeypatch):
    seen: list[Event] = []

    def fake_pinned(family, *, emit=None, cancel=None):
        emit(Event("progress", "download: 1 MB / 2 MB", progress=0.5))
        raise RuntimeError("stop here, Steam is not touched in this test")

    monkeypatch.setattr(runners, "ensure_pinned", fake_pinned)
    act = steamglue.make_actions("3224770", root=tmp_path, emit=seen.append)
    item = steamsetup.SetupItem(key="runner", label="Runner", state="todo", action="set_runner")
    result = act(item)
    assert not result.ok
    assert [e.progress for e in seen] == [0.5]


def test_the_dialog_shows_progress_from_a_worker_thread(monkeypatch):
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable")
    from nadare.gui.steam_setup import SteamSetupDialog

    app = QApplication.instance() or QApplication([])
    dialog = SteamSetupDialog(None, plan_fn=lambda: None, act_fn=lambda item: None)
    try:
        dialog._busy = True
        worker = threading.Thread(
            target=dialog.report_progress, args=("Downloading DW-Proton: 42 %",)
        )
        worker.start()
        worker.join()
        app.processEvents()
        assert dialog.result_label.text() == "Downloading DW-Proton: 42 %"
        dialog._busy = False
        dialog.report_progress("late")
        app.processEvents()
        assert dialog.result_label.text() != "late"  # a finished step keeps its result line
    finally:
        dialog.deleteLater()
