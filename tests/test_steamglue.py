# SPDX-License-Identifier: GPL-3.0-or-later
"""The glue between the Set up planner/actions and the real machine (folders, processes)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nadare.core import steamglue, steamsetup
from nadare.core.backends import BackendError

UMA = "3224770"


def _steam(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir()
    return root


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---- finding Steam ------------------------------------------------------------------------
def test_the_steam_root_is_the_first_folder_that_holds_a_steamapps_dir(tmp_path):
    root = _steam(tmp_path)
    assert steamglue.steam_root(home=tmp_path) == root


def test_a_flatpak_steam_is_found_when_the_native_one_is_absent(tmp_path):
    flat = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (flat / "steamapps").mkdir(parents=True)
    assert steamglue.steam_root(home=tmp_path) == flat


def test_no_steam_is_none_not_an_error(tmp_path):
    assert steamglue.steam_root(home=tmp_path) is None


def test_the_steam_files_are_found_under_the_root(tmp_path):
    root = _steam(tmp_path)
    (root / "config" / "config.vdf").write_text("x")
    for account in ("111", "222"):
        cfg = root / "userdata" / account / "config"
        cfg.mkdir(parents=True)
        (cfg / "localconfig.vdf").write_text(f"account {account}")
    assert steamglue.config_vdf(root) == root / "config" / "config.vdf"
    assert sorted(steamglue.localconfig_texts(root)) == ["account 111", "account 222"]
    assert steamglue.compat_text(root) == "x"
    assert steamglue.tool_dir(root) == root / "compatibilitytools.d"


def test_missing_files_read_as_empty(tmp_path):
    root = _steam(tmp_path)
    assert steamglue.localconfig_texts(root) == []
    assert steamglue.compat_text(root) is None


# ---- making DW-Proton visible to Steam ----------------------------------------------------
def test_a_runner_is_linked_into_steams_tool_folder_under_its_own_name(tmp_path):
    root = _steam(tmp_path)
    runner = tmp_path / "nadare-runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    name = steamglue.ensure_steam_tool(root, runner)
    assert name == "dwproton-11.0-15-x86_64"
    link = root / "compatibilitytools.d" / name
    assert link.is_symlink() and link.resolve() == runner.resolve()
    assert steamglue.ensure_steam_tool(root, runner) == name  # idempotent


def test_a_runner_already_in_steams_folder_is_used_where_it_is(tmp_path):
    root = _steam(tmp_path)
    runner = root / "compatibilitytools.d" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    assert steamglue.ensure_steam_tool(root, runner) == runner.name
    assert not runner.is_symlink()


def test_an_existing_different_entry_is_never_replaced(tmp_path):
    root = _steam(tmp_path)
    other = root / "compatibilitytools.d" / "dwproton-11.0-15-x86_64"
    other.mkdir(parents=True)
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    with pytest.raises(BackendError, match="different tool"):
        steamglue.ensure_steam_tool(root, runner)
    assert other.is_dir() and not other.is_symlink()


# ---- running commands ---------------------------------------------------------------------
def test_a_quick_command_returns_its_exit_code(tmp_path):
    ok = _script(tmp_path / "ok", "exit 0\n")
    bad = _script(tmp_path / "bad", "exit 4\n")
    assert steamglue.run([str(ok)]) == 0
    assert steamglue.run([str(bad)]) == 4


def test_a_command_that_stays_up_is_left_running_and_counts_as_started(tmp_path):
    marker = tmp_path / "pid"
    slow = _script(tmp_path / "slow", f"echo $$ > {marker}\nsleep 30\n")
    code = steamglue.run([str(slow)], settle=0.5)
    assert code == 0
    pid = int(marker.read_text())
    try:
        os.kill(pid, 0)  # still alive: it was not killed or waited for
    finally:
        try:
            os.killpg(os.getpgid(pid), 15)  # its own session: stops the shell and the sleep
        except ProcessLookupError:
            pass


def test_a_missing_program_is_a_plain_failure_not_a_crash(tmp_path):
    assert steamglue.run([str(tmp_path / "does-not-exist")]) == 127


def test_waiting_for_steam_to_go_returns_as_soon_as_it_is_gone():
    calls = iter([True, True, False])
    assert steamglue.wait_until_gone(5.0, running=lambda: next(calls), poll=0.0) is True


def test_waiting_for_steam_gives_up_after_the_timeout():
    assert steamglue.wait_until_gone(0.05, running=lambda: True, poll=0.01) is False


# ---- the planner and the dispatcher -------------------------------------------------------
def test_the_plan_reads_the_real_files_under_the_root(tmp_path):
    root = _steam(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(
        '"X"\n{\n\t"streaming"\n\t{\n\t\t"UnlockedH264Feature"\t\t"1"\n\t}\n}\n'
    )
    plan = steamglue.plan_for(UMA, root=root, vulkan_version="1.4.305")
    assert plan.next_item is None  # installed, unlocked, runner only optional
    assert {item.key: item.state for item in plan.items}["h264"] == "done"


def test_with_no_steam_the_plan_says_so_in_plain_words(tmp_path):
    with pytest.raises(BackendError, match="Steam"):
        steamglue.plan_for(UMA, root=None, vulkan_version=None)


def _item(action: str, key: str = "x") -> steamsetup.SetupItem:
    return steamsetup.SetupItem(key, "label", steamsetup.STATE_TODO, action=action)


def test_the_dispatcher_runs_the_action_the_item_names(tmp_path):
    calls: list[list[str]] = []
    root = _steam(tmp_path)
    account = root / "userdata" / "1" / "config"
    account.mkdir(parents=True)
    (account / "localconfig.vdf").write_text(
        '"X"\n{\n\t"streaming"\n\t{\n\t\t"UnlockedH264Feature"\t\t"1"\n\t}\n}\n'
    )
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: calls.append(argv) or 0, confirm_timeout=1.0,
    )
    assert act(_item("steam_install")).ok
    assert act(_item("unlock_h264")).ok
    assert calls[0][-1] == f"steam://install/{UMA}"
    assert calls[1][-1] == "steam://unlockh264/"


def test_an_unknown_action_is_refused_not_guessed(tmp_path):
    act = steamglue.make_actions(UMA, root=_steam(tmp_path), run=lambda argv: 0)
    result = act(_item("rm -rf"))
    assert not result.ok


def test_the_runner_action_links_the_tool_and_edits_the_config(tmp_path):
    root = _steam(tmp_path)
    (root / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
        '\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
    )
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True, ensure_runner_fn=lambda: runner,
    )
    result = act(_item("set_runner", "runner"))
    assert result.ok and result.changed
    assert (root / "compatibilitytools.d" / runner.name).is_symlink()
    text = (root / "config" / "config.vdf").read_text()
    assert f'"{UMA}"' in text and '"dwproton-11.0-15-x86_64"' in text


def test_the_runner_action_without_steam_is_a_plain_refusal():
    act = steamglue.make_actions(UMA, root=None, run=lambda argv: 0)
    result = act(_item("set_runner", "runner"))
    assert not result.ok and "Steam" in result.line


def test_a_failing_runner_download_is_reported_not_raised(tmp_path):
    def boom():
        raise RuntimeError("no network")

    act = steamglue.make_actions(
        UMA, root=_steam(tmp_path), run=lambda argv: 0, ensure_runner_fn=boom,
    )
    result = act(_item("set_runner", "runner"))
    assert not result.ok and "no network" in result.line


def test_the_prefix_action_hands_the_app_and_key_to_the_installer(tmp_path):
    seen: list[tuple[str, str]] = []

    def installer(app_id, key):
        seen.append((app_id, key))
        return steamglue.steamrun.ActionResult(True, "done")

    act = steamglue.make_actions(
        "5019180", root=_steam(tmp_path), run=lambda argv: 0, install_prefix_fn=installer
    )
    assert act(_item("install_prefix:webview2")).ok
    assert seen == [("5019180", "webview2")]


def test_a_prefix_action_without_an_installer_says_it_is_not_available(tmp_path):
    act = steamglue.make_actions("5019180", root=_steam(tmp_path), run=lambda argv: 0)
    result = act(_item("install_prefix:webview2"))
    assert not result.ok and "not available" in result.line


def test_libraries_listed_by_steam_are_searched_too(tmp_path):
    root = _steam(tmp_path)
    extra = tmp_path / "games"
    (extra / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t\t"{extra}"\n\t}}\n}}\n'
    )
    assert steamglue.steamapps_dirs_for(root) == [root / "steamapps", extra / "steamapps"]


# ---- the H.264 step waits until Steam has written the setting down ------------------------
def _account(root: Path) -> Path:
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    path = cfg / "localconfig.vdf"
    path.write_text('"X"\n{\n\t"streaming"\n\t{\n\t\t"UnlockedH264Feature"\t\t"0"\n\t}\n}\n')
    return path


def test_the_unlock_step_waits_until_steam_has_recorded_the_setting(tmp_path):
    import threading

    root = _steam(tmp_path)
    path = _account(root)

    def run(argv):
        # Steam answers the request at once but writes its settings a moment later.
        threading.Timer(
            0.3, lambda: path.write_text(path.read_text().replace('"0"', '"1"'))
        ).start()
        return 0

    act = steamglue.make_actions(UMA, root=root, run=run, confirm_timeout=5.0, poll=0.05)
    result = act(_item("unlock_h264", "h264"))
    assert result.ok
    assert steamsetup.h264_unlocked(steamglue.localconfig_texts(root))  # already on disk


def test_a_steam_that_never_records_it_is_reported_honestly(tmp_path):
    root = _steam(tmp_path)
    _account(root)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 0, confirm_timeout=0.2, poll=0.05
    )
    result = act(_item("unlock_h264", "h264"))
    assert not result.ok
    assert "confirm" in result.line.lower() and "again" in result.line.lower()


def test_a_failed_unlock_request_does_not_wait(tmp_path):
    import time

    root = _steam(tmp_path)
    _account(root)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 5, confirm_timeout=30.0, poll=0.05
    )
    started = time.monotonic()
    result = act(_item("unlock_h264", "h264"))
    assert not result.ok and time.monotonic() - started < 5.0


def test_without_a_steam_root_the_unlock_is_not_checked(tmp_path):
    act = steamglue.make_actions(UMA, root=None, run=lambda argv: 0, confirm_timeout=0.1)
    assert act(_item("unlock_h264", "h264")).ok  # nothing to read, the request is all we have


# ---- Flatpak, accounts, link failures -----------------------------------------------------
def test_a_flatpak_steam_is_started_through_flatpak(tmp_path):
    flat = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (flat / "steamapps").mkdir(parents=True)
    assert steamglue.default_binary(flat, home=tmp_path) == "flatpak run com.valvesoftware.Steam"
    native = _steam(tmp_path)
    assert steamglue.default_binary(native, home=tmp_path) is None
    assert steamglue.default_binary(None, home=tmp_path) is None


def test_the_profiles_own_steam_command_wins(tmp_path):
    flat = tmp_path / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (flat / "steamapps").mkdir(parents=True)
    assert steamglue.default_binary(flat, home=tmp_path, configured="/opt/steam") == "/opt/steam"
    assert steamglue.default_binary(flat, home=tmp_path, configured="  ") == (
        "flatpak run com.valvesoftware.Steam"
    )


def _two_accounts(root: Path) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"76561198313531351"\n\t{\n\t\t"MostRecent"\t\t"0"\n\t}\n'
        '\t"76561198000000042"\n\t{\n\t\t"MostRecent"\t\t"1"\n\t}\n}\n'
    )
    for number, flag in ((76561198313531351 - 76561197960265728, "1"),
                         (76561198000000042 - 76561197960265728, "0")):
        cfg = root / "userdata" / str(number) / "config"
        cfg.mkdir(parents=True)
        (cfg / "localconfig.vdf").write_text(
            f'"X"\n{{\n\t"streaming"\n\t{{\n\t\t"UnlockedH264Feature"\t\t"{flag}"\n\t}}\n}}\n'
        )


def test_h264_is_read_for_the_account_that_is_signed_in_not_for_any_account(tmp_path):
    root = _steam(tmp_path)
    _two_accounts(root)  # the OTHER account unlocked it, the signed-in one did not
    texts = steamglue.active_localconfig_texts(root)
    assert len(texts) == 1
    assert not steamsetup.h264_unlocked(texts)
    assert steamsetup.h264_unlocked(steamglue.localconfig_texts(root))  # what any-account says


def test_a_single_account_without_loginusers_is_that_account(tmp_path):
    root = _steam(tmp_path)
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text("only account")
    assert steamglue.active_localconfig_texts(root) == ["only account"]


def test_several_accounts_and_no_way_to_tell_which_means_not_unlocked(tmp_path):
    root = _steam(tmp_path)
    for number in ("1", "2"):
        cfg = root / "userdata" / number / "config"
        cfg.mkdir(parents=True)
        (cfg / "localconfig.vdf").write_text(f"account {number}")
    assert steamglue.active_localconfig_texts(root) == []


def test_the_plan_uses_the_signed_in_account(tmp_path):
    root = _steam(tmp_path)
    _two_accounts(root)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    plan = steamglue.plan_for(UMA, root=root, vulkan_version=None)
    states = {item.key: item.state for item in plan.items}
    assert states["h264"] == "todo"


def test_a_link_that_cannot_be_made_is_reported_not_swallowed(tmp_path):
    root = _steam(tmp_path)
    tools = root / "compatibilitytools.d"
    tools.mkdir()
    tools.chmod(0o500)  # read-only: the symlink cannot be created
    if os.access(tools, os.W_OK):
        tools.chmod(0o700)
        pytest.skip("directory permissions do not apply to this user (root)")
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    try:
        with pytest.raises(BackendError, match="visible to Steam"):
            steamglue.ensure_steam_tool(root, runner)
    finally:
        tools.chmod(0o700)


# ---- the native Wayland step reads and writes the signed-in account's launch options ------
LAUNCH_FILE = (
    '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
    '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n'
    '\t\t\t\t\t\t"LastPlayed"\t\t"1"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
)


def _proton(path: Path, wayland: bool) -> Path:
    unix = path / "files" / "lib" / "wine" / "x86_64-unix"
    unix.mkdir(parents=True)
    if wayland:
        (unix / "winewayland.so").write_bytes(b"")
    (path / "proton").write_text("#!/bin/sh\n")
    return path / "proton"


def test_the_active_accounts_file_is_found_by_path_too(tmp_path):
    root = _steam(tmp_path)
    _two_accounts(root)
    path = steamglue.active_localconfig_path(root)
    assert path == root / "userdata" / str(76561198000000042 - 76561197960265728) / "config" / (
        "localconfig.vdf"
    )
    assert steamglue.active_localconfig_path(_steam(tmp_path / "empty")) is None


def test_the_plan_offers_wayland_when_session_and_runner_allow_it(tmp_path):
    root = _steam(tmp_path)
    (root / "steamapps" / f"appmanifest_{UMA}.acf").write_text('"AppState"\n{\n}\n')
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(LAUNCH_FILE)
    _proton(root / "steamapps" / "common" / "Proton - Experimental", wayland=False)
    plan = steamglue.plan_for(UMA, root=root, vulkan_version="1.4", wayland_session=True)
    wayland = next(i for i in plan.items if i.key == "wayland")
    assert wayland.action == "" and "Wayland driver" in wayland.detail  # Valve's has none

    (root / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
        '\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"3224770"\n\t\t\t\t\t{\n'
        '\t\t\t\t\t\t"name"\t\t"dw"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
    )
    _proton(root / "compatibilitytools.d" / "dw", wayland=True)
    plan = steamglue.plan_for(UMA, root=root, vulkan_version="1.4", wayland_session=True)
    assert next(i for i in plan.items if i.key == "wayland").action == "set_wayland"

    (cfg / "localconfig.vdf").write_text(
        LAUNCH_FILE.replace(
            '"LastPlayed"\t\t"1"', '"LaunchOptions"\t\t"PROTON_ENABLE_WAYLAND=1 %command%"'
        )
    )
    plan = steamglue.plan_for(UMA, root=root, vulkan_version="1.4", wayland_session=True)
    assert next(i for i in plan.items if i.key == "wayland").state == "done"
    plan = steamglue.plan_for(UMA, root=root, vulkan_version="1.4", wayland_session=False)
    assert all(i.key != "wayland" for i in plan.items)


def test_the_wayland_action_edits_the_signed_in_accounts_file(tmp_path):
    root = _steam(tmp_path)
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(LAUNCH_FILE)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True,
    )
    result = act(_item("set_wayland", "wayland"))
    assert result.ok and result.changed
    assert "PROTON_ENABLE_WAYLAND=1 %command%" in (cfg / "localconfig.vdf").read_text()


def test_the_wayland_action_without_a_known_account_is_a_plain_refusal(tmp_path):
    root = _steam(tmp_path)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True,
    )
    result = act(_item("set_wayland", "wayland"))
    assert not result.ok and "account" in result.line.lower()


# ---- Cancel: the runner download can be stopped, and Steam is then left alone -------------
def _runner_config(root: Path) -> None:
    (root / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
        '\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
    )


def test_the_runner_download_gets_the_cancel_token(tmp_path, monkeypatch):
    from nadare.core.events import Cancel

    root = _steam(tmp_path)
    _runner_config(root)
    seen: dict = {}
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    monkeypatch.setattr(
        steamglue.runners, "ensure_pinned",
        lambda family, **kw: seen.update(kw) or runner,
    )
    token = Cancel()
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True, cancel=token,
    )
    assert act(_item("set_runner", "runner")).ok
    assert seen["cancel"] is token


def test_a_cancelled_runner_download_changes_nothing_and_leaves_steam_alone(tmp_path, monkeypatch):
    from nadare.core.events import Cancel, Cancelled

    root = _steam(tmp_path)
    _runner_config(root)
    before = (root / "config" / "config.vdf").read_text()
    calls: list[list[str]] = []

    def cancelled(family, **kw):
        raise Cancelled()

    monkeypatch.setattr(steamglue.runners, "ensure_pinned", cancelled)
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: calls.append(argv) or 0, steam_running=lambda: True,
        wait_gone=lambda t: True, cancel=Cancel(),
    )
    result = act(_item("set_runner", "runner"))
    assert not result.ok and result.line == "Cancelled. Nothing was changed."  # not a "failure"
    assert (root / "config" / "config.vdf").read_text() == before
    assert calls == []  # Steam was never closed


def test_a_cancel_after_the_download_stops_before_steam_is_closed(tmp_path, monkeypatch):
    from nadare.core.events import Cancel

    root = _steam(tmp_path)
    _runner_config(root)
    before = (root / "config" / "config.vdf").read_text()
    token = Cancel()
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)

    def finished_then_cancelled(family, **kw):
        token.cancel()  # the user pressed Cancel just as the download finished
        return runner

    monkeypatch.setattr(steamglue.runners, "ensure_pinned", finished_then_cancelled)
    calls: list[list[str]] = []
    act = steamglue.make_actions(
        UMA, root=root, run=lambda argv: calls.append(argv) or 0, steam_running=lambda: True,
        wait_gone=lambda t: True, cancel=token,
    )
    result = act(_item("set_runner", "runner"))
    assert not result.ok and "Cancelled" in result.line
    assert (root / "config" / "config.vdf").read_text() == before and calls == []


# ---- the runner step downloads the family that suits the game --------------------------------
@pytest.mark.parametrize(
    ("app", "family"), [("3308670", "ge"), (UMA, "dwproton"), ("5019180", "dwproton")]
)
def test_the_runner_step_fetches_the_runner_family_of_that_game(tmp_path, monkeypatch, app, family):
    root = _steam(tmp_path)
    _runner_config(root)
    asked: list[str] = []
    runner = tmp_path / "runners" / f"{family}-x86_64"
    runner.mkdir(parents=True)
    monkeypatch.setattr(
        steamglue.runners, "ensure_pinned", lambda fam, **kw: asked.append(fam) or runner
    )
    act = steamglue.make_actions(
        app, root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True,
    )
    assert act(_item("set_runner", "runner")).ok
    assert asked == [family]


def test_the_wayland_action_passes_the_extra_variables_of_that_game(tmp_path):
    root = _steam(tmp_path)
    cfg = root / "userdata" / "1" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(LAUNCH_FILE.replace("3224770", "3308670"))
    act = steamglue.make_actions(
        "3308670", root=root, run=lambda argv: 0, steam_running=lambda: False,
        wait_gone=lambda t: True,
    )
    assert act(_item("set_wayland", "wayland")).ok
    text = (cfg / "localconfig.vdf").read_text()
    assert "PROTON_ENABLE_WAYLAND=1 UMU_ID=0 %command%" in text
