# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the GUI review findings of 2026-11-08."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_steam_setup_gui import _close, _dialog, _item, _plan, _wait, qapp  # noqa: F401

from nadare.core import runners, steamsetup
from nadare.core.config import Config
from nadare.core.steamrun import ActionResult
from nadare.gui.settings import SettingsPage, _is_runner_folder


def test_a_re_check_after_a_finished_step_offers_no_cancel(qapp):  # noqa: F811
    started, release = threading.Event(), threading.Event()
    plan = _plan(
        [_item("runner", "DW-Proton runner", steamsetup.STATE_TODO, action="set_runner")]
    )

    def act_fn(item):
        started.set()
        release.wait(5)
        return ActionResult(True, "Steam now uses DW-Proton.", changed=True)

    dialog = _dialog(lambda: plan, act_fn, cancel_fn=lambda: None)
    try:
        dialog.next_button.click()
        assert _wait(qapp, started.is_set)
        release.set()
        assert _wait(qapp, lambda: not dialog.busy)
        # The finished step left its action name behind; a re-check is not that step.
        assert dialog._running_action == "set_runner"
        gate = threading.Event()

        def slow_plan():
            gate.wait(5)
            return plan

        dialog._plan_fn = slow_plan
        dialog.check_again()
        assert not dialog._can_cancel()
        assert dialog.cancel_button.isHidden()
        gate.set()
        assert _wait(qapp, lambda: not dialog.busy)
    finally:
        gate = locals().get("gate")
        if gate is not None:
            gate.set()
        release.set()
        _close(dialog, qapp)


def test_close_is_enabled_during_a_re_check(qapp):  # noqa: F811
    plan = _plan([_item("installed", "Installed", steamsetup.STATE_DONE)])
    dialog = _dialog(lambda: plan)
    try:
        dialog._busy = True
        dialog._rechecking = True
        dialog._apply_enabled()
        assert dialog.close_button.isEnabled()
    finally:
        dialog._busy = False
        dialog._rechecking = False
        _close(dialog, qapp)


def test_a_wuwa_quality_change_is_sent_as_a_notice_not_a_box(tmp_path, qapp):  # noqa: F811
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "w", backend="direct-wuwa", install_dir=str(tmp_path / "wuwa"), wuwa_quality="hd"
    )
    page = SettingsPage()
    notices: list[tuple[str, str]] = []
    page.notice.connect(lambda profile, text: notices.append((profile, text)))
    try:
        page.load(config, "w")
        combo = page.option_widget("wuwa_quality")
        combo.setCurrentIndex(combo.findData("sd"))
        page.save()
        assert config.profile("w")["wuwa_quality"] == "sd"
        assert notices and notices[-1][0] == "w"
        assert "SD" in notices[-1][1] and "HD" in notices[-1][1]
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_and_the_launcher_agree_on_what_a_runner_folder_is(tmp_path):
    only_bin = tmp_path / "bin-only"
    (only_bin / "bin").mkdir(parents=True)
    (only_bin / "bin" / "wine").write_text("")
    files_bin = tmp_path / "files-bin"
    (files_bin / "files" / "bin").mkdir(parents=True)
    (files_bin / "files" / "bin" / "wine").write_text("")
    proton = tmp_path / "proton"
    proton.mkdir()
    (proton / "proton").write_text("")

    assert not _is_runner_folder(str(only_bin))
    assert not runners.looks_like_runner(only_bin)
    assert _is_runner_folder(str(files_bin))
    assert runners.looks_like_runner(files_bin)
    assert _is_runner_folder(str(proton))
    assert runners.looks_like_runner(proton)
