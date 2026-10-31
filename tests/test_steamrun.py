# SPDX-License-Identifier: GPL-3.0-or-later
"""The Set up actions: what they run, in which order, and what they refuse (all injected)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import steamrun, steamtools
from nadare.core.backends import BackendError

CONFIG = """"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"3224770"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"proton_experimental"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


class Recorder:
    """A fake process runner and a fake Steam client that remembers what happened."""

    def __init__(self, *, exit_code: int = 0, steam_up: bool = True, stops_on_shutdown=True):
        self.calls: list[list[str]] = []
        self.exit_code = exit_code
        self.up = steam_up
        self.stops = stops_on_shutdown
        self.waits: list[float] = []

    def run(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        if argv[-1] == "-shutdown" and self.stops:
            self.up = False
        if "steam://" not in argv[-1] and argv[-1] != "-shutdown":
            self.up = True  # a plain `steam` start
        return self.exit_code

    def running(self) -> bool:
        return self.up

    def wait_gone(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return not self.up


# ---- steam:// requests --------------------------------------------------------------------
def test_installing_asks_steam_for_that_app_and_nothing_else():
    rec = Recorder()
    result = steamrun.request_steam_install("3224770", run=rec.run)
    assert result.ok
    assert rec.calls == [["steam", "steam://install/3224770"]]


@pytest.mark.parametrize("bad", ["", "abc", "12 34", "1;rm", "-1"])
def test_a_bad_app_id_never_reaches_steam(bad):
    rec = Recorder()
    with pytest.raises(BackendError):
        steamrun.request_steam_install(bad, run=rec.run)
    assert rec.calls == []


def test_the_steam_command_may_carry_arguments_such_as_flatpak():
    rec = Recorder()
    steamrun.request_steam_install(
        "5019180", run=rec.run, binary="flatpak run com.valvesoftware.Steam"
    )
    assert rec.calls == [["flatpak", "run", "com.valvesoftware.Steam", "steam://install/5019180"]]


def test_unlock_h264_runs_the_clients_own_request_once():
    rec = Recorder()
    result = steamrun.unlock_h264(run=rec.run)
    assert result.ok and rec.calls == [steamtools.unlockh264_command()]
    assert "H.264" in result.line


def test_a_failed_steam_command_is_reported_plainly():
    rec = Recorder(exit_code=3)
    result = steamrun.unlock_h264(run=rec.run)
    assert not result.ok and "3" in result.line


# ---- the runner change --------------------------------------------------------------------
def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.vdf"
    path.write_text(CONFIG)
    return path


def test_the_runner_change_closes_steam_edits_one_entry_and_starts_steam_again(tmp_path):
    path = _config(tmp_path)
    rec = Recorder()
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok
    assert rec.calls[0] == ["steam", "-shutdown"]
    assert rec.calls[-1] == ["steam"]
    text = path.read_text()
    assert steamtools.read_compat_entry(text, "3224770").tool == "dwproton-11.0-15"
    assert result.backup is not None and result.backup.is_file()
    assert result.previous is not None and result.previous.tool == "proton_experimental"


def test_steam_that_does_not_close_stops_everything_and_changes_nothing(tmp_path):
    path = _config(tmp_path)
    rec = Recorder(stops_on_shutdown=False)
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert not result.ok and "close" in result.line.lower()
    assert path.read_text() == CONFIG
    assert ["steam"] not in rec.calls  # not started again on top of a running one
    assert not list(tmp_path.glob("config.vdf.bak*"))


def test_a_steam_that_was_not_running_is_not_started_afterwards(tmp_path):
    path = _config(tmp_path)
    rec = Recorder(steam_up=False)
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok and rec.calls == []


def test_a_missing_config_is_reported_and_steam_is_left_as_it_was(tmp_path):
    rec = Recorder()
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=tmp_path / "nope.vdf", run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert not result.ok
    assert ["steam"] in rec.calls  # it was closed for the attempt, so it is started again


def test_the_runner_change_can_be_undone_to_the_exact_previous_entry(tmp_path):
    path = _config(tmp_path)
    rec = Recorder()
    done = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    rec2 = Recorder()
    undone = steamrun.restore_runner(
        "3224770", done.previous, config_path=path, run=rec2.run,
        steam_running=rec2.running, wait_gone=rec2.wait_gone,
    )
    assert undone.ok
    assert path.read_text() == CONFIG
    assert rec2.calls[0] == ["steam", "-shutdown"]


def test_an_already_chosen_runner_changes_and_backs_up_nothing(tmp_path):
    path = _config(tmp_path)
    rec = Recorder(steam_up=False)
    result = steamrun.set_runner(
        "3224770", "proton_experimental", config_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok and not result.changed
    assert not list(tmp_path.glob("config.vdf.bak*"))


# ---- finding the Proton a game uses -------------------------------------------------------
def _proton(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "proton").write_text("#!/bin/sh\n")
    return path / "proton"


def test_the_proton_of_the_games_own_setting_is_found(tmp_path):
    steamapps = tmp_path / "steamapps"
    expected = _proton(steamapps / "common" / "Proton - Experimental")
    found = steamrun.find_proton(
        "3224770", CONFIG, steamapps_dirs=[steamapps], tool_dirs=[tmp_path / "tools"]
    )
    assert found == expected


def test_a_custom_runner_is_looked_up_in_the_tool_folders(tmp_path):
    tools = tmp_path / "tools"
    expected = _proton(tools / "dwproton-11.0-15")
    text = CONFIG.replace("proton_experimental", "dwproton-11.0-15")
    found = steamrun.find_proton(
        "3224770", text, steamapps_dirs=[tmp_path / "steamapps"], tool_dirs=[tools]
    )
    assert found == expected


def test_a_game_without_a_setting_uses_proton_experimental_when_present(tmp_path):
    steamapps = tmp_path / "steamapps"
    expected = _proton(steamapps / "common" / "Proton - Experimental")
    found = steamrun.find_proton("999", CONFIG, steamapps_dirs=[steamapps], tool_dirs=[])
    assert found == expected


def test_no_proton_at_all_is_none(tmp_path):
    assert steamrun.find_proton("999", "", steamapps_dirs=[tmp_path], tool_dirs=[tmp_path]) is None


# ---- running an installer in the game's prefix --------------------------------------------
def test_the_proton_command_runs_in_the_games_own_prefix(tmp_path):
    proton = tmp_path / "proton"
    argv, env = steamrun.proton_run_command(
        proton=proton, compat_data=tmp_path / "compatdata" / "5019180",
        client_path=tmp_path / "Steam", args=["setup.exe", "/silent"], base_env={"DISPLAY": ":0"},
    )
    assert argv == [str(proton), "run", "setup.exe", "/silent"]
    assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "compatdata" / "5019180")
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(tmp_path / "Steam")
    assert env["DISPLAY"] == ":0"


def test_the_prefix_install_refuses_while_the_game_runs(tmp_path):
    called = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=tmp_path / "pfx",
        ensure_fn=lambda *a, **k: called.append(1),
        running_blockers=lambda: ["Epic Seven is running"],
        backup_fn=lambda *a, **k: called.append("backup"),
    )
    assert not result.ok and "running" in result.line.lower()
    assert called == []


def test_the_prefix_install_backs_up_first_and_then_installs(tmp_path):
    order: list[str] = []
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix,
        ensure_fn=lambda: order.append("install"),
        running_blockers=lambda: [],
        backup_fn=lambda src, dest: order.append("backup"),
    )
    assert result.ok and order == ["backup", "install"]


def test_a_failed_install_keeps_the_backup_and_says_where_it_is(tmp_path):
    prefix = tmp_path / "pfx"
    prefix.mkdir()

    def boom():
        raise BackendError("the installer failed")

    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=boom,
        running_blockers=lambda: [], backup_fn=lambda src, dest: None,
    )
    assert not result.ok and "failed" in result.line
    assert "backup" in result.line.lower()


def test_only_known_prefix_fixes_can_run(tmp_path):
    with pytest.raises(BackendError):
        steamrun.install_prefix(
            "5019180", "rm-rf", prefix=tmp_path, ensure_fn=lambda: None,
            running_blockers=lambda: [], backup_fn=lambda *a: None,
        )


# ---- review fixes: Steam must come back, ids and names must be plain ----------------------
def test_any_failure_of_the_edit_still_starts_steam_again(tmp_path):
    rec = Recorder()

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=_config(tmp_path), run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone, apply=boom,
    )
    assert not result.ok and "space" in result.line
    assert rec.calls[-1] == ["steam"]


def test_the_prefix_install_only_takes_a_numeric_app_id(tmp_path):
    for bad in ("../x", "5019180/..", "", "a b"):
        with pytest.raises(BackendError):
            steamrun.install_prefix(
                bad, "webview2", prefix=tmp_path, ensure_fn=lambda: None,
                running_blockers=lambda: [], backup_fn=lambda *a: None,
            )


@pytest.mark.parametrize("name", ["../evil", "a/b", "..", ".", "x\x00y", ""])
def test_a_runner_name_with_path_pieces_is_never_looked_up(tmp_path, name):
    tools = tmp_path / "tools"
    tools.mkdir()
    _proton(tmp_path / "evil")  # what "../evil" would reach from the tool folder
    _proton(tools / "a" / "b")
    text = CONFIG.replace("proton_experimental", name or "x")
    found = steamrun.find_proton(
        "3224770", text, steamapps_dirs=[tmp_path / "steamapps"], tool_dirs=[tools]
    )
    assert found is None


def test_the_backup_goes_next_to_the_prefix_under_the_agreed_name(tmp_path):
    compatdata = tmp_path / "compatdata" / "5019180"
    prefix = compatdata / "pfx"
    prefix.mkdir(parents=True)
    seen: list[tuple[Path, Path]] = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: None,
        running_blockers=lambda: [], backup_fn=lambda src, dest: seen.append((src, dest)),
    )
    assert result.ok
    assert seen == [(prefix, prefix.parent / "5019180.bak-pre-webview2")]
    assert result.backup == prefix.parent / "5019180.bak-pre-webview2"


def test_a_failed_steam_install_request_is_reported_with_its_exit_code():
    rec = Recorder(exit_code=7)
    result = steamrun.request_steam_install("5019180", run=rec.run)
    assert not result.ok and "7" in result.line


def test_a_named_proton_that_is_not_installed_is_not_swapped_for_experimental(tmp_path):
    steamapps = tmp_path / "steamapps"
    _proton(steamapps / "common" / "Proton - Experimental")  # present, but not what was chosen
    text = CONFIG.replace("proton_experimental", "proton_9")
    assert steamrun.find_proton(
        "3224770", text, steamapps_dirs=[steamapps], tool_dirs=[tmp_path / "tools"]
    ) is None


# ---- scrutiny fixes -----------------------------------------------------------------------
class FailingStart(Recorder):
    """A Steam whose restart command fails (no `steam` on the path, no display, ...)."""

    def run(self, argv: list[str]) -> int:
        super().run(argv)
        return 5 if argv == ["steam"] else 0


def test_a_failed_restart_is_told_to_the_user_but_the_change_stays_reported(tmp_path):
    rec = FailingStart()
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=_config(tmp_path), run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok and result.changed  # the setting really changed
    assert "could not start Steam again" in result.line and "5" in result.line


def test_a_failed_restart_after_a_refused_edit_is_told_too(tmp_path):
    rec = FailingStart()
    result = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=tmp_path / "nope.vdf", run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert not result.ok and "could not start Steam again" in result.line


def test_two_runner_changes_never_run_at_the_same_time(tmp_path):
    import threading

    path = _config(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class Blocking(Recorder):
        def run(self, argv):
            if argv[-1] == "-shutdown":
                entered.set()
                release.wait(5.0)
            return super().run(argv)

    first, second = Blocking(), Recorder()
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            steamrun.set_runner(
                "3224770", "dwproton-11.0-15", config_path=path, run=first.run,
                steam_running=first.running, wait_gone=first.wait_gone,
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(5.0)
        blocked = steamrun.set_runner(
            "5019180", "dwproton-11.0-15", config_path=path, run=second.run,
            steam_running=second.running, wait_gone=second.wait_gone,
        )
        assert not blocked.ok and "already" in blocked.line.lower()
        assert second.calls == []  # it did not even touch Steam
    finally:
        release.set()
        worker.join(5.0)
    assert outcome and outcome[0].ok


def test_the_lock_is_free_again_after_a_change_even_a_failed_one(tmp_path):
    rec = Recorder(steam_up=False)
    for _ in range(2):
        result = steamrun.set_runner(
            "3224770", "dwproton-11.0-15", config_path=tmp_path / "nope.vdf", run=rec.run,
            steam_running=rec.running, wait_gone=rec.wait_gone,
        )
        assert "already" not in result.line.lower()


def test_the_installer_is_not_started_if_the_game_appears_during_the_backup(tmp_path):
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    answers = iter([[], ["Epic Seven is running"]])
    installed: list[int] = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: installed.append(1),
        running_blockers=lambda: next(answers), backup_fn=lambda src, dest: None,
    )
    assert installed == []
    assert not result.ok and "started while" in result.line
    assert result.backup == prefix.parent / "5019180.bak-pre-webview2"


def test_a_failing_backup_stops_everything_and_says_so(tmp_path):
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    installed: list[int] = []

    def full_disk(src, dest):
        raise BackendError("no space left on device")

    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: installed.append(1),
        running_blockers=lambda: [], backup_fn=full_disk,
    )
    assert installed == []
    assert not result.ok and "back up" in result.line.lower() and "no space" in result.line


def test_a_successful_prefix_install_reports_a_change_and_the_backup(tmp_path):
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: None,
        running_blockers=lambda: [], backup_fn=lambda src, dest: None,
    )
    assert result.ok and result.changed
    assert result.backup == prefix.parent / "5019180.bak-pre-webview2"


# ---- which Proton a game uses -------------------------------------------------------------
DEFAULT_ENTRY = CONFIG.replace('"3224770"', '"0"').replace(
    "proton_experimental", "proton_8"
)


def test_steams_default_runner_is_used_when_the_game_has_no_entry_of_its_own(tmp_path):
    steamapps = tmp_path / "steamapps"
    _proton(steamapps / "common" / "Proton - Experimental")
    expected = _proton(steamapps / "common" / "Proton 8.0")
    found = steamrun.find_proton(
        "999", DEFAULT_ENTRY, steamapps_dirs=[steamapps], tool_dirs=[tmp_path / "tools"]
    )
    assert found == expected


def test_a_missing_default_runner_is_not_swapped_for_experimental(tmp_path):
    steamapps = tmp_path / "steamapps"
    _proton(steamapps / "common" / "Proton - Experimental")
    assert steamrun.find_proton(
        "999", DEFAULT_ENTRY, steamapps_dirs=[steamapps], tool_dirs=[]
    ) is None


def test_a_missing_custom_runner_is_not_swapped_for_experimental(tmp_path):
    steamapps = tmp_path / "steamapps"
    _proton(steamapps / "common" / "Proton - Experimental")
    text = CONFIG.replace("proton_experimental", "GE-Proton11-7")
    assert steamrun.find_proton(
        "3224770", text, steamapps_dirs=[steamapps], tool_dirs=[tmp_path / "tools"]
    ) is None


def test_the_games_own_entry_beats_the_default(tmp_path):
    steamapps = tmp_path / "steamapps"
    own = _proton(steamapps / "common" / "Proton - Experimental")
    _proton(steamapps / "common" / "Proton 8.0")
    both = CONFIG.replace(
        '\t\t\t\t\t"3224770"',
        '\t\t\t\t\t"0"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton_8"\n\t\t\t\t\t}\n'
        '\t\t\t\t\t"3224770"',
    )
    assert steamrun.find_proton(
        "3224770", both, steamapps_dirs=[steamapps], tool_dirs=[]
    ) == own


@pytest.mark.parametrize(
    ("tool", "folder"),
    [
        ("proton_experimental", "Proton - Experimental"),
        ("proton_hotfix", "Proton Hotfix"),
        ("proton_10", "Proton 10.0"),
        ("proton_9", "Proton 9.0"),
        ("proton_8", "Proton 8.0"),
        ("proton_7", "Proton 7.0"),
        ("proton_63", "Proton 6.3"),
        ("proton_513", "Proton 5.13"),
        ("proton_411", "Proton 4.11"),
        ("proton_42", "Proton 4.2"),
        ("proton_37", "Proton 3.7"),
    ],
)
def test_steams_tool_names_map_to_their_folder_names(tool, folder):
    assert steamrun._display_name(tool) == folder


# ---- preflight before the backup ----------------------------------------------------------
def test_a_failed_preflight_stops_before_any_backup_is_made(tmp_path):
    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    prefix.mkdir(parents=True)
    made: list[object] = []
    ran: list[object] = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: ran.append(1),
        running_blockers=lambda: [], backup_fn=lambda *a: made.append(a),
        preflight=lambda: "No display found.",
    )
    assert not result.ok and "No display found." in result.line
    assert made == [] and ran == [] and result.backup is None


def test_a_passing_preflight_lets_the_install_go_on(tmp_path):
    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    prefix.mkdir(parents=True)
    made: list[object] = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: None,
        running_blockers=lambda: [], backup_fn=lambda *a: made.append(a),
        preflight=lambda: None,
    )
    assert result.ok and len(made) == 1


def test_undoing_a_game_that_had_no_setting_says_steams_default_is_back(tmp_path):
    config = tmp_path / "config.vdf"
    config.write_text(CONFIG.replace('"3224770"', '"1"'))  # no entry for the game
    rec = Recorder()
    changed = steamrun.set_runner(
        "3224770", "dwproton-11.0-15", config_path=config, run=rec.run,
        steam_running=lambda: False, wait_gone=lambda s: True,
    )
    assert changed.ok and changed.previous is None
    undone = steamrun.restore_runner(
        "3224770", changed.previous, config_path=config, run=rec.run,
        steam_running=lambda: False, wait_gone=lambda s: True,
    )
    assert undone.ok
    assert "for app" not in undone.line or "Steam now uses  " not in undone.line
    assert "default" in undone.line


# ---- the native Wayland launch option -----------------------------------------------------
LOCAL_CONFIG = (
    '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
    '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n'
    '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
)


def _local(tmp_path: Path) -> Path:
    path = tmp_path / "localconfig.vdf"
    path.write_text(LOCAL_CONFIG)
    return path


def test_the_wayland_step_closes_steam_sets_the_option_and_starts_steam_again(tmp_path):
    from nadare.core import steamwayland

    path = _local(tmp_path)
    rec = Recorder()
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok and result.changed
    assert rec.calls[0] == ["steam", "-shutdown"] and rec.calls[-1] == ["steam"]
    assert steamtools.read_launch_options(path.read_text(), "3224770") == (
        "PROTON_ENABLE_WAYLAND=1 %command%"
    )
    assert result.backup is not None and result.backup.read_text() == LOCAL_CONFIG
    assert result.previous == steamwayland.LaunchUndo(None)
    assert "Wayland" in result.line


def test_the_wayland_step_keeps_the_players_own_options(tmp_path):
    path = _local(tmp_path)
    path.write_text(LOCAL_CONFIG.replace(
        '"LastPlayed"\t\t"1"', '"LaunchOptions"\t\t"mangohud %command%"'
    ))
    rec = Recorder(steam_up=False)
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert steamtools.read_launch_options(path.read_text(), "3224770") == (
        "PROTON_ENABLE_WAYLAND=1 mangohud %command%"
    )
    assert rec.calls == []  # Steam was not running, so it is not started either
    assert result.previous.previous == "mangohud %command%"


def test_the_wayland_step_can_be_undone_to_exactly_what_was_there(tmp_path):
    path = _local(tmp_path)
    rec = Recorder()
    done = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    undone = steamrun.restore_wayland(
        "3224770", done.previous, localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert undone.ok
    assert path.read_text() == LOCAL_CONFIG


def test_steam_that_does_not_close_leaves_the_launch_options_alone(tmp_path):
    path = _local(tmp_path)
    rec = Recorder(stops_on_shutdown=False)
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert not result.ok and path.read_text() == LOCAL_CONFIG


def test_a_wayland_game_that_is_already_set_changes_nothing(tmp_path):
    path = _local(tmp_path)
    path.write_text(LOCAL_CONFIG.replace(
        '"LastPlayed"\t\t"1"', '"LaunchOptions"\t\t"PROTON_ENABLE_WAYLAND=1 %command%"'
    ))
    rec = Recorder(steam_up=False)
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok and not result.changed and result.backup is None


# ---- Cancel while a prefix install is being prepared --------------------------------------
def test_a_cancel_after_the_backup_stops_before_the_installer_and_keeps_the_backup(tmp_path):
    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    prefix.mkdir(parents=True)
    ran: list[int] = []
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: ran.append(1),
        running_blockers=lambda: [], backup_fn=lambda *a: None, cancelled=lambda: True,
    )
    assert not result.ok and ran == []
    assert "Cancelled" in result.line and "backup" in result.line.lower()
    assert result.backup == prefix.parent / "5019180.bak-pre-webview2"


def test_a_cancelled_installer_download_is_reported_as_cancelled_not_failed(tmp_path):
    from nadare.core.events import Cancelled

    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    prefix.mkdir(parents=True)

    def ensure():
        raise Cancelled()

    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=ensure,
        running_blockers=lambda: [], backup_fn=lambda *a: None,
    )
    assert not result.ok and "Cancelled" in result.line and "failed" not in result.line.lower()
    assert result.backup is not None


def test_not_cancelled_installs_as_before(tmp_path):
    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    prefix.mkdir(parents=True)
    result = steamrun.install_prefix(
        "5019180", "webview2", prefix=prefix, ensure_fn=lambda: None,
        running_blockers=lambda: [], backup_fn=lambda *a: None, cancelled=lambda: False,
    )
    assert result.ok


def test_the_wayland_step_refuses_while_the_game_is_running(tmp_path):
    path = _local(tmp_path)
    rec = Recorder()
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run,
        steam_running=rec.running, wait_gone=rec.wait_gone,
        game_running=lambda: True,
    )
    assert not result.ok and not result.changed
    assert "still running" in result.line
    assert rec.calls == [], "Steam must not be closed or restarted"
    assert path.read_text() == LOCAL_CONFIG


def test_the_runner_step_and_both_undos_refuse_while_the_game_is_running(tmp_path):
    from nadare.core import steamwayland

    path = _local(tmp_path)
    rec = Recorder()
    common = dict(run=rec.run, steam_running=rec.running, wait_gone=rec.wait_gone,
                  game_running=lambda: True)
    results = [
        steamrun.set_runner("3224770", "proton_experimental", config_path=path, **common),
        steamrun.restore_runner("3224770", None, config_path=path, **common),
        steamrun.restore_wayland(
            "3224770", steamwayland.LaunchUndo(None), localconfig_path=path, **common
        ),
    ]
    assert all(not r.ok and "still running" in r.line for r in results)
    assert rec.calls == []
    assert path.read_text() == LOCAL_CONFIG


def test_the_wayland_step_writes_the_games_extra_variables_too(tmp_path):
    path = _local(tmp_path)
    rec = Recorder(steam_up=False)
    result = steamrun.set_wayland(
        "3224770", localconfig_path=path, run=rec.run, extra_env=("UMU_ID=0",),
        steam_running=rec.running, wait_gone=rec.wait_gone,
    )
    assert result.ok
    assert steamtools.read_launch_options(path.read_text(), "3224770") == (
        "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%"
    )
