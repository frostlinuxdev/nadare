# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1627: the Set up window says what is enough, and the flicker rationale is gone."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401 - imported for the fixture below
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_steam_setup_gui import _close, _dialog, _item, _plan, qapp  # noqa: F401
from test_steamsetup import LOCALCONFIG_ON, UMA, _library

from nadare.core import steamsetup

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"


def test_no_obsolete_flicker_rationale_is_left_in_the_source():
    hits = []
    for path in SRC.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue  # bytecode of older checkouts is not source
        try:
            if "flicker" in path.read_text(errors="ignore").lower():
                hits.append(str(path.relative_to(SRC)))
        except OSError:  # pragma: no cover - unreadable file: not source text
            continue
    assert hits == []


def test_a_ready_plan_says_so_at_the_top_and_the_button_closes(qapp):  # noqa: F811
    plan = _plan(
        [
            _item("installed", "Installed in Steam", steamsetup.STATE_DONE),
            _item(
                "runner",
                "DW-Proton runner",
                steamsetup.STATE_OPTIONAL,
                detail="Optional: only if the game does not start.",
                action="set_runner",
                action_label="Use DW-Proton",
            ),
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert plan.ready
        assert dialog.status_label.text() == "Ready to play - the steps below are optional."
        assert dialog.next_button.text() == "Close"
        assert "Do next step" not in dialog.next_button.text()
        assert dialog.next_button.isEnabled()
        # The optional row is not hidden: it is the user's choice, not required work.
        assert set(dialog.rows) == {"installed", "runner"}
        assert dialog.rows["runner"].detail_label.text() == (
            "Optional: only if the game does not start."
        )
    finally:
        _close(dialog, qapp)


def test_a_plan_with_required_steps_left_counts_and_names_them(qapp):  # noqa: F811
    plan = _plan(
        [
            _item(
                "installed",
                "Installed in Steam",
                steamsetup.STATE_TODO,
                action="steam_install",
                action_label="Install in Steam",
            ),
            _item(
                "h264",
                "H.264 video unlocked",
                steamsetup.STATE_TODO,
                action="unlock_h264",
                action_label="Unlock H.264 video",
            ),
            _item(
                "runner",
                "DW-Proton runner",
                steamsetup.STATE_OPTIONAL,
                action="set_runner",
                action_label="Use DW-Proton",
            ),
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert not plan.ready
        text = dialog.status_label.text()
        assert text.startswith("2 steps left:")
        assert "install in Steam, then unlock H.264" in text
        assert dialog.next_button.text() == "Install in Steam"
    finally:
        _close(dialog, qapp)


def test_one_required_step_is_singular(qapp):  # noqa: F811
    plan = _plan(
        [
            _item("installed", "Installed in Steam", steamsetup.STATE_DONE),
            _item(
                "h264",
                "H.264 video unlocked",
                steamsetup.STATE_TODO,
                action="unlock_h264",
                action_label="Unlock H.264 video",
            ),
            _item("runner", "DW-Proton runner", steamsetup.STATE_OPTIONAL, action="set_runner"),
        ]
    )
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.status_label.text() == "1 step left: unlock H.264"
    finally:
        _close(dialog, qapp)


def test_the_real_planner_keeps_optional_rows_with_a_short_detail(tmp_path, qapp):  # noqa: F811
    _library(tmp_path, UMA)
    plan = steamsetup.plan_setup(
        UMA,
        steamapps_dirs=[tmp_path / "lib" / "steamapps"],
        localconfig_texts=[LOCALCONFIG_ON],
        wayland_session=True,
        runner_has_wayland=True,
    )
    assert plan.ready
    dialog = _dialog(lambda: plan)
    try:
        assert {"runner", "wayland"} <= set(dialog.rows)
        for key in ("runner", "wayland"):
            assert dialog.rows[key].detail_label.text() == (
                "Optional: only if the game does not start."
            )
    finally:
        _close(dialog, qapp)


def test_the_wayland_step_keeps_its_reason_without_mentioning_flicker(tmp_path):
    _library(tmp_path, UMA)
    plan = steamsetup.plan_setup(
        UMA,
        steamapps_dirs=[tmp_path / "lib" / "steamapps"],
        localconfig_texts=[LOCALCONFIG_ON],
        wayland_session=True,
        runner_has_wayland=False,
    )
    item = next(item for item in plan.items if item.key == "wayland")
    assert "Wayland driver" in item.detail and "DW-Proton" in item.detail
    assert "flicker" not in (item.detail + item.warning).lower()


def test_haoplay_says_nadare_installs_it_directly_now():
    note = steamsetup.game_for("3308670").note.lower()
    assert "direct" in note and "nadare" in note
    assert "steam" in note  # the Steam steps stay for people who own it there


def test_one_close_button_when_nothing_is_left_to_do(qapp):  # noqa: F811
    """#1627: with no step left the primary button is "Close", so the footer one goes."""
    plan = _plan([_item("installed", "Installed in Steam", steamsetup.STATE_DONE)])
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.next_button.text() == "Close"
        assert dialog.close_button.isHidden()
    finally:
        _close(dialog, qapp)


def test_the_footer_close_stays_while_a_step_is_left(qapp):  # noqa: F811
    todo = _item("h264", "H.264 video unlocked", steamsetup.STATE_TODO, action="unlock_h264")
    plan = _plan([todo])
    dialog = _dialog(lambda: plan)
    try:
        assert dialog.next_button.text() != "Close"
        assert not dialog.close_button.isHidden()
    finally:
        _close(dialog, qapp)
