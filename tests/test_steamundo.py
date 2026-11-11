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


def test_what_nadare_wrote_is_saved_with_a_launch_option_undo(tmp_path):
    undo = steamwayland.LaunchUndo("mangohud %command%", written="X=1 mangohud %command%")
    steamundo.save("5019180", "set_wayland", undo, state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", undo)


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


def test_the_newest_change_is_the_one_offered_first(tmp_path):
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
    assert [entry["action"] for entry in data["3224770"]] == ["set_runner"]  # a stack, #1031
    assert not list(tmp_path.glob("*.tmp*")) and not list(tmp_path.glob(".*"))


# ---- an undo belongs to the Steam account it was made for (#894) -------------------------------
def test_the_account_file_is_remembered_with_the_undo(tmp_path):
    undo = steamwayland.LaunchUndo("--previous-A")
    where = "/steam/userdata/1/config/localconfig.vdf"
    steamundo.save("5019180", "set_wayland", undo, where=where, state_dir=tmp_path)
    assert steamundo.where("5019180", state_dir=tmp_path) == where
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", undo)


def test_an_undo_fits_the_account_it_was_saved_for(tmp_path):
    undo = steamwayland.LaunchUndo("x")
    steamundo.save("1", "set_wayland", undo, where="/a/localconfig.vdf", state_dir=tmp_path)
    assert steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)
    assert not steamundo.fits("1", "/b/localconfig.vdf", state_dir=tmp_path)


def test_an_older_launch_option_undo_without_an_account_is_not_trusted(tmp_path):
    steamundo.save("1", "set_wayland", steamwayland.LaunchUndo("x"), state_dir=tmp_path)
    assert not steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)


def test_a_runner_undo_fits_only_the_steam_install_it_was_saved_for(tmp_path):
    # #970: native Steam and Flatpak Steam each have their own config.vdf.
    steamundo.save("1", "set_runner", ENTRY, where="/native/config.vdf", state_dir=tmp_path)
    assert steamundo.fits("1", "/native/config.vdf", state_dir=tmp_path)
    assert not steamundo.fits("1", "/flatpak/config.vdf", state_dir=tmp_path)


def test_an_older_runner_undo_without_an_install_is_not_trusted(tmp_path):
    steamundo.save("1", "set_runner", ENTRY, state_dir=tmp_path)
    assert not steamundo.fits("1", "/anything", state_dir=tmp_path)


def test_no_undo_fits_nothing(tmp_path):
    assert not steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)


# ---- a second change no longer throws the first one's undo away (#1031) --------------------
def test_undos_stack_so_each_change_can_be_taken_back_in_turn(tmp_path):
    runner = steamtools.CompatEntry("5019180", {"name": "proton_9"}, "text")
    launch = steamwayland.LaunchUndo("mangohud %command%", written="X=1 mangohud %command%")
    steamundo.save("5019180", "set_runner", runner, where="/s/config.vdf", state_dir=tmp_path)
    steamundo.save("5019180", "set_wayland", launch, where="/s/local.vdf", state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", launch)
    assert steamundo.where("5019180", state_dir=tmp_path) == "/s/local.vdf"
    steamundo.clear("5019180", state_dir=tmp_path)  # the newest was undone
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_runner", runner)
    assert steamundo.where("5019180", state_dir=tmp_path) == "/s/config.vdf"
    steamundo.clear("5019180", state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) is None


def test_an_undo_file_from_before_the_stack_still_loads(tmp_path):
    import json

    (tmp_path / steamundo.FILE).write_text(json.dumps({"7": {
        "action": "set_wayland", "payload": {"kind": "launch", "previous": None},
        "where": "/s/local.vdf"}}))
    assert steamundo.load("7", state_dir=tmp_path) == ("set_wayland", steamwayland.LaunchUndo(None))
    steamundo.save("7", "set_wayland", steamwayland.LaunchUndo("a"), where="/x", state_dir=tmp_path)
    steamundo.clear("7", state_dir=tmp_path)
    assert steamundo.load("7", state_dir=tmp_path) == ("set_wayland", steamwayland.LaunchUndo(None))


def test_the_stack_keeps_only_the_last_few(tmp_path):
    for n in range(12):
        steamundo.save("9", "set_wayland", steamwayland.LaunchUndo(str(n)), where="/w",
                       state_dir=tmp_path)
    seen = []
    while (got := steamundo.load("9", state_dir=tmp_path)) is not None:
        seen.append(got[1].previous)
        steamundo.clear("9", state_dir=tmp_path)
    assert seen == [str(n) for n in range(11, 11 - steamundo.KEEP, -1)]
