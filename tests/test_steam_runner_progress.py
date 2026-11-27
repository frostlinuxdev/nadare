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


def test_set_up_shows_the_runner_unpack_step_not_a_frozen_100_percent(
    tmp_path, monkeypatch
):
    """The runner download ends at 100%, then extracting has only a step line to show."""
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable")
    from nadare.core.steamrun import ActionResult
    from nadare.gui import steam_wiring

    app = QApplication.instance() or QApplication([])
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir()
    captured: dict = {}

    def fake_make_actions(app_id, **kwargs):
        captured["emit"] = kwargs["emit"]
        return lambda item: ActionResult(True, "done")

    monkeypatch.setattr(steam_wiring.steamglue, "make_actions", fake_make_actions)
    dialog = steam_wiring.build_dialog(
        None,
        "3224770",
        root=root,
        vulkan_version_fn=lambda: None,
        async_plan=False,
        confirm_fn=lambda *args: True,
    )
    try:
        dialog._busy = True
        item = steamsetup.SetupItem(
            key="runner", label="Runner", state="todo", action="set_runner"
        )
        dialog._act_fn(item)
        captured["emit"](Event("progress", "download: 1 MB / 2 MB", 0.5))
        app.processEvents()
        assert dialog.result_label.text() == "Downloading the runner: 50 %"
        captured["emit"](
            Event("step", "Unpacking the runner…", data={"stage": "extract"})
        )
        app.processEvents()
        assert dialog.result_label.text() == "Unpacking the runner…"
    finally:
        dialog.deleteLater()
        app.processEvents()
