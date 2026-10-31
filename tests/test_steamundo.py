# SPDX-License-Identifier: GPL-3.0-or-later
"""The undo of a Set up change is kept on disk, so it still works after Nadare restarts."""

from __future__ import annotations

import json

from nadare.core import steamtools, steamundo, steamwayland

ENTRY = steamtools.CompatEntry("3224770", {"name": "proton_experimental", "config": ""}, "BLOCK")


def test_a_runner_undo_comes_back_exactly(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    action, payload = steamundo.load("3224770", state_dir=tmp_path)
    assert action == "set_runner"
    assert payload == ENTRY and payload.text == "BLOCK" and payload.tool == "proton_experimental"


def test_a_game_that_had_no_runner_setting_comes_back_as_none(tmp_path):
    steamundo.save("3224770", "set_runner", None, state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) == ("set_runner", None)


def test_a_launch_option_undo_comes_back_exactly(tmp_path):
    for previous in ("mangohud %command%", None, ""):
        undo = steamwayland.LaunchUndo(previous)
        steamundo.save("5019180", "set_wayland", undo, state_dir=tmp_path)
        assert steamundo.load("5019180", state_dir=tmp_path) == (
            "set_wayland", steamwayland.LaunchUndo(previous),
        )


def test_each_game_keeps_its_own_undo_and_clear_removes_only_that_one(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    steamundo.save("5019180", "set_wayland", steamwayland.LaunchUndo(None), state_dir=tmp_path)
    steamundo.clear("3224770", state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    assert steamundo.load("5019180", state_dir=tmp_path) is not None
    steamundo.clear("999", state_dir=tmp_path)  # nothing saved: not an error


def test_a_new_change_replaces_the_games_old_undo(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    steamundo.save("3224770", "set_wayland", steamwayland.LaunchUndo("x"), state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path)[0] == "set_wayland"


def test_a_missing_or_damaged_file_is_just_no_undo(tmp_path):
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    for text in ("", "not json", "[]", '{"3224770": 5}', '{"3224770": {"action": "x"}}',
                 '{"3224770": {"action": "set_runner", "payload": {"kind": "weird"}}}'):
        (tmp_path / steamundo.FILE).write_text(text)
        assert steamundo.load("3224770", state_dir=tmp_path) is None
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)  # heals over the damage
    assert steamundo.load("3224770", state_dir=tmp_path) is not None


def test_only_the_two_undoable_steps_are_saved(tmp_path):
    steamundo.save("3224770", "unlock_h264", None, state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    assert not (tmp_path / steamundo.FILE).exists()


def test_the_file_is_plain_json_written_whole(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    data = json.loads((tmp_path / steamundo.FILE).read_text())
    assert data["3224770"]["action"] == "set_runner"
    assert not list(tmp_path.glob("*.tmp*")) and not list(tmp_path.glob(".*"))
