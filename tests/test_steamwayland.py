# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Wayland for a Steam game: the launch option, its editor and the runner check."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import steamtools, steamwayland
from nadare.core.backends import BackendError

LOCAL = (
    '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
    '\t\t\t\t"apps"\n\t\t\t\t{\n'
    '\t\t\t\t\t"5019180"\n\t\t\t\t\t{\n\t\t\t\t\t\t"LastPlayed"\t\t"1"\n\t\t\t\t\t}\n'
    '\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n\t\t\t\t\t\t"LaunchOptions"\t\t"mangohud %command% -x"\n'
    '\t\t\t\t\t\t"Playtime"\t\t"5"\n\t\t\t\t\t}\n'
    "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n"
)


# ---- the launch option text ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", "PROTON_ENABLE_WAYLAND=1 %command%"),
        ("mangohud %command% -x", "PROTON_ENABLE_WAYLAND=1 mangohud %command% -x"),
        ("-windowed", "PROTON_ENABLE_WAYLAND=1 %command% -windowed"),
        ("PROTON_ENABLE_WAYLAND=0 %command%", "PROTON_ENABLE_WAYLAND=1 %command%"),
        ("FOO=1 PROTON_ENABLE_WAYLAND=0 %command%", "FOO=1 PROTON_ENABLE_WAYLAND=1 %command%"),
        ("PROTON_ENABLE_WAYLAND=1 %command%", "PROTON_ENABLE_WAYLAND=1 %command%"),
    ],
)
def test_the_wayland_launch_option_keeps_what_the_player_already_had(before, after):
    assert steamwayland.with_wayland(before) == after


def test_wayland_is_on_only_when_the_option_says_one():
    assert steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=1 %command%")
    assert steamwayland.has_wayland("FOO=2 PROTON_ENABLE_WAYLAND=1 mangohud %command%")
    assert not steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=0 %command%")
    assert not steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=10 %command%")
    assert not steamwayland.has_wayland("MY_PROTON_ENABLE_WAYLAND=1 %command%")
    assert not steamwayland.has_wayland("")
    assert not steamwayland.has_wayland(None)


# ---- the runner and the session --------------------------------------------------------------
def _runner(root: Path, wayland: bool) -> Path:
    unix = root / "files" / "lib" / "wine" / "x86_64-unix"
    unix.mkdir(parents=True)
    (unix / "winex11.so").write_bytes(b"")
    if wayland:
        (unix / "winewayland.so").write_bytes(b"")
    (root / "proton").write_text("#!/bin/sh\n")
    return root / "proton"


def test_a_runner_can_do_wayland_only_with_the_wayland_driver(tmp_path):
    assert steamwayland.runner_has_wayland(_runner(tmp_path / "dw", True))
    assert not steamwayland.runner_has_wayland(_runner(tmp_path / "valve", False))
    assert not steamwayland.runner_has_wayland(None)
    assert not steamwayland.runner_has_wayland(tmp_path / "missing" / "proton")


def test_a_wayland_session_is_told_from_the_environment():
    assert steamwayland.wayland_session({"XDG_SESSION_TYPE": "wayland"})
    assert steamwayland.wayland_session({"WAYLAND_DISPLAY": "wayland-0"})
    assert not steamwayland.wayland_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    assert not steamwayland.wayland_session({})


# ---- reading and editing the account's launch options ----------------------------------------
def test_launch_options_are_read_per_game():
    assert steamtools.read_launch_options(LOCAL, "3224770") == "mangohud %command% -x"
    assert steamtools.read_launch_options(LOCAL, "5019180") is None  # a game with none
    assert steamtools.read_launch_options(LOCAL, "999") is None  # a game Steam never saw
    assert steamtools.read_launch_options("", "5019180") is None


def test_setting_an_option_changes_only_that_game_and_keeps_every_other_byte():
    out = steamtools.edit_launch_options_text(LOCAL, "3224770", "PROTON_ENABLE_WAYLAND=1 %command%")
    assert out == LOCAL.replace("mangohud %command% -x", "PROTON_ENABLE_WAYLAND=1 %command%")


def test_a_game_with_no_option_gets_one_inside_its_own_block():
    out = steamtools.edit_launch_options_text(LOCAL, "5019180", "A=1 %command%")
    assert steamtools.read_launch_options(out, "5019180") == "A=1 %command%"
    assert steamtools.read_launch_options(out, "3224770") == "mangohud %command% -x"
    assert out.count("LastPlayed") == 1 and out.count("Playtime") == 1


def test_a_game_steam_has_no_block_for_gets_a_new_block():
    out = steamtools.edit_launch_options_text(LOCAL, "3557620", "A=1 %command%")
    assert steamtools.read_launch_options(out, "3557620") == "A=1 %command%"
    assert steamtools.read_launch_options(out, "5019180") is None


def test_removing_an_option_puts_the_file_back_exactly():
    added = steamtools.edit_launch_options_text(LOCAL, "5019180", "A=1 %command%")
    assert steamtools.edit_launch_options_text(added, "5019180", None) == LOCAL.replace(
        '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n', '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n'
    )
    assert steamtools.read_launch_options(
        steamtools.edit_launch_options_text(LOCAL, "3224770", None), "3224770"
    ) is None


def test_quotes_and_backslashes_in_an_option_are_escaped():
    value = 'A="x y" B=C:\\dir %command%'
    out = steamtools.edit_launch_options_text(LOCAL, "5019180", value)
    assert steamtools.read_launch_options(out, "5019180") == value


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (LOCAL.replace('\t\t\t\t"apps"', '\t\t\t\t"appz"'), "apps"),
        (
            LOCAL.replace('"LastPlayed"\t\t"1"', '"LaunchOptions" "a" "LaunchOptions" "b"'),
            "more than one",
        ),
    ],
)
def test_a_file_that_does_not_look_right_is_refused(text, message):
    with pytest.raises(BackendError, match=message):
        steamtools.edit_launch_options_text(text, "5019180", "A=1 %command%")


def test_a_cut_off_game_block_is_refused():
    cut = LOCAL[: LOCAL.index('\t\t\t\t\t\t"LastPlayed"') + 40]
    with pytest.raises(BackendError, match="not closed|apps"):
        steamtools.edit_launch_options_text(cut, "5019180", "A=1 %command%")


# ---- the file operation ----------------------------------------------------------------------
def test_applying_writes_a_backup_first_and_reports_what_was_there(tmp_path):
    path = tmp_path / "localconfig.vdf"
    path.write_text(LOCAL)
    edit = steamtools.apply_launch_options(
        path, "3224770", "PROTON_ENABLE_WAYLAND=1 mangohud %command% -x",
        steam_running_fn=lambda: False, lock_dir=tmp_path / "locks",
    )
    assert edit.changed and edit.previous == "mangohud %command% -x"
    assert edit.backup is not None and edit.backup.read_text() == LOCAL
    assert steamtools.read_launch_options(path.read_text(), "3224770").startswith("PROTON_")


def test_applying_the_same_option_again_writes_nothing(tmp_path):
    path = tmp_path / "localconfig.vdf"
    path.write_text(LOCAL)
    edit = steamtools.apply_launch_options(
        path, "3224770", "mangohud %command% -x",
        steam_running_fn=lambda: False, lock_dir=tmp_path / "locks",
    )
    assert not edit.changed and edit.backup is None
    assert path.read_text() == LOCAL and not list(tmp_path.glob("*.bak*"))


def test_it_refuses_while_steam_runs_because_steam_rewrites_the_file_on_exit(tmp_path):
    path = tmp_path / "localconfig.vdf"
    path.write_text(LOCAL)
    with pytest.raises(BackendError, match="Steam is running"):
        steamtools.apply_launch_options(
            path, "3224770", "A=1 %command%",
            steam_running_fn=lambda: True, lock_dir=tmp_path / "locks",
        )
    assert path.read_text() == LOCAL


# ---- extra launch variables a game needs together with native Wayland -------------------------
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%"),
        ("mangohud %command% -x", "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 mangohud %command% -x"),
        ("-windowed", "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command% -windowed"),
        # what the player wrote wins: an UMU_ID already there is kept as it is
        ("UMU_ID=7 %command%", "PROTON_ENABLE_WAYLAND=1 UMU_ID=7 %command%"),
        (
            "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%",
            "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%",
        ),
        ("PROTON_ENABLE_WAYLAND=1 %command%", "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%"),
        ("MY_UMU_ID=3 %command%", "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 MY_UMU_ID=3 %command%"),
    ],
)
def test_a_game_can_ask_for_more_variables_than_wayland(before, after):
    assert steamwayland.with_wayland(before, extra=("UMU_ID=0",)) == after


def test_without_extras_nothing_changes():
    assert steamwayland.with_wayland("mangohud %command%") == (
        "PROTON_ENABLE_WAYLAND=1 mangohud %command%"
    )
    assert steamwayland.with_wayland("mangohud %command%", extra=()) == (
        "PROTON_ENABLE_WAYLAND=1 mangohud %command%"
    )


def test_only_name_equals_value_pairs_are_accepted_as_extras():
    for bad in ("UMU_ID", "=1", "A B=1", "A=1;rm", "$(x)=1", "A=`x`", ""):
        with pytest.raises(ValueError):
            steamwayland.with_wayland("", extra=(bad,))


# ---- only the environment before %command% counts, game arguments stay as they are (#897) ------
def test_an_argument_after_the_command_is_not_an_environment_setting():
    assert steamwayland.has_wayland("%command% --note PROTON_ENABLE_WAYLAND=1") is False
    assert steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=1 %command% --x") is True
    assert steamwayland.has_wayland("MANGOHUD=1 PROTON_ENABLE_WAYLAND=1 %command%") is True
    assert steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=0 %command%") is False


def test_a_game_argument_that_looks_like_the_setting_is_kept_and_a_real_one_is_added():
    out = steamwayland.with_wayland("%command% --note PROTON_ENABLE_WAYLAND=0")
    assert out == "PROTON_ENABLE_WAYLAND=1 %command% --note PROTON_ENABLE_WAYLAND=0"
    assert steamwayland.has_wayland(out) is True


def test_a_real_setting_before_the_command_is_turned_on_in_place():
    out = steamwayland.with_wayland("A=1 PROTON_ENABLE_WAYLAND=0 B=2 %command% --keep")
    assert out == "A=1 PROTON_ENABLE_WAYLAND=1 B=2 %command% --keep"


def test_a_duplicate_in_the_arguments_is_left_when_the_real_one_is_set():
    out = steamwayland.with_wayland("PROTON_ENABLE_WAYLAND=0 %command% PROTON_ENABLE_WAYLAND=0")
    assert out == "PROTON_ENABLE_WAYLAND=1 %command% PROTON_ENABLE_WAYLAND=0"


def test_options_without_a_command_are_arguments_and_get_a_wrapper():
    assert steamwayland.with_wayland("--windowed") == "PROTON_ENABLE_WAYLAND=1 %command% --windowed"
    assert steamwayland.with_wayland("PROTON_ENABLE_WAYLAND=0") == (
        "PROTON_ENABLE_WAYLAND=1 %command% PROTON_ENABLE_WAYLAND=0"
    )
    assert steamwayland.with_wayland("") == "PROTON_ENABLE_WAYLAND=1 %command%"
    assert steamwayland.with_wayland(None) == "PROTON_ENABLE_WAYLAND=1 %command%"


def test_extra_variables_only_look_before_the_command():
    out = steamwayland.with_wayland("%command% UMU_ID=7", extra=("UMU_ID=0",))
    assert out == "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command% UMU_ID=7"
    keep = steamwayland.with_wayland("UMU_ID=5 %command%", extra=("UMU_ID=0",))
    assert keep == "PROTON_ENABLE_WAYLAND=1 UMU_ID=5 %command%"


# ---- repeated assignments: the shell keeps the last one (#970) --------------------------------
def test_the_effective_setting_is_the_last_assignment_before_the_command():
    assert not steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=1 PROTON_ENABLE_WAYLAND=0 %command%")
    assert steamwayland.has_wayland("PROTON_ENABLE_WAYLAND=0 PROTON_ENABLE_WAYLAND=1 %command%")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "PROTON_ENABLE_WAYLAND=0 PROTON_ENABLE_WAYLAND=0 %command%",
            "PROTON_ENABLE_WAYLAND=1 %command%",
        ),
        (
            "PROTON_ENABLE_WAYLAND=1 PROTON_ENABLE_WAYLAND=0 %command%",
            "PROTON_ENABLE_WAYLAND=1 %command%",
        ),
        (
            "FOO=1 PROTON_ENABLE_WAYLAND=0 BAR=2 PROTON_ENABLE_WAYLAND=0 %command% -x",
            "FOO=1 PROTON_ENABLE_WAYLAND=1 BAR=2 %command% -x",
        ),
    ],
)
def test_turning_wayland_on_leaves_one_assignment_so_the_game_really_gets_one(before, after):
    result = steamwayland.with_wayland(before)
    assert result == after
    assert steamwayland.has_wayland(result)


def test_variables_the_game_needs_are_told_from_what_is_set_before_the_command():
    has = steamwayland.has_variables
    assert has("UMU_ID=0 PROTON_ENABLE_WAYLAND=1 %command%", ("UMU_ID=0",))
    assert has("UMU_ID=5 %command%", ("UMU_ID=0",))  # the player's own value counts
    assert not has("PROTON_ENABLE_WAYLAND=1 %command%", ("UMU_ID=0",))
    assert not has("%command% UMU_ID=0", ("UMU_ID=0",))  # an argument is not a setting
    assert has("anything", ())
