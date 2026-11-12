# SPDX-License-Identifier: GPL-3.0-or-later
"""The Set up ladder for a Steam game: what is done, what is to do, what is offered (pure)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import steamfix, steamsetup, steamtools
from nadare.core.backends import BackendError

EPIC = "5019180"
UMA = "3224770"
BLUE = "3557620"

COMPAT_TEXT = """"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"%s"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"%s"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""

_HEAD = '"UserLocalConfigStore"\n{\n\t"streaming"\n\t{\n\t\t'
LOCALCONFIG_ON = _HEAD + '"UnlockedH264Feature"\t\t"1"\n\t}\n}\n'
LOCALCONFIG_OFF = _HEAD + '"EnableStreaming"\t\t"1"\n\t}\n}\n'


def _library(root: Path, app_id: str, *, pfx: bool = True) -> Path:
    steamapps = root / "lib" / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / f"appmanifest_{app_id}.acf").write_text('"AppState"\n{\n}\n')
    if pfx:
        (steamapps / "compatdata" / app_id / "pfx" / "drive_c").mkdir(parents=True, exist_ok=True)
    return steamapps


_WEBVIEW2_REG = (
    "WINE REGISTRY Version 2\n\n"
    "[Software\\\\Wow6432Node\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\"
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}] 1\n"
    '"pv"="150.0.1"\n'
)


def _webview2(steamapps: Path, app_id: str) -> None:
    app = (
        steamapps / "compatdata" / app_id / "pfx" / "drive_c"
        / "Program Files (x86)/Microsoft/EdgeWebView/Application/150.0.1"
    )
    app.mkdir(parents=True)
    (app / "msedgewebview2.exe").write_bytes(b"MZ")
    (steamapps / "compatdata" / app_id / "pfx" / "system.reg").write_text(_WEBVIEW2_REG)


def _plan(app_id: str, tmp_path: Path, **kw):
    kw.setdefault("steamapps_dirs", [tmp_path / "lib" / "steamapps"])
    return steamsetup.plan_setup(app_id, **kw)


def _item(plan, key: str):
    return next(item for item in plan.items if item.key == key)


# ---- the game table --------------------------------------------------------------------
def test_the_table_knows_the_four_steam_games_with_their_evidence():
    epic = steamsetup.game_for(EPIC)
    assert epic.needs == (steamfix.KEY_WEBVIEW2,) and not epic.video and epic.verified
    uma = steamsetup.game_for(UMA)
    assert uma.video and uma.needs == () and uma.verified
    blue = steamsetup.game_for(BLUE)
    assert not blue.video and blue.needs == () and blue.verified
    assert steamsetup.game_for("3347400").verified is False  # GFL2 Darkwinter: region-locked
    assert steamsetup.game_for("3308670").game.endswith("(Haoplay)")  # the Haoplay build
    assert steamsetup.game_for("999") is None


def test_the_table_agrees_with_the_prefix_fixer_and_the_h264_list():
    assert set(steamsetup.game_for(EPIC).needs) <= set(steamfix.PREREQUISITE_KEYS)
    assert steamtools.h264_game(UMA) is not None
    assert steamsetup.game_for(UMA).video


# ---- reading Steam's settings ------------------------------------------------------------
def test_h264_unlock_is_read_from_the_account_config():
    assert steamtools.read_flag(LOCALCONFIG_ON, "UnlockedH264Feature") == "1"
    assert steamtools.read_flag(LOCALCONFIG_OFF, "UnlockedH264Feature") is None
    assert steamtools.read_flag("", "UnlockedH264Feature") is None
    assert steamsetup.h264_unlocked([LOCALCONFIG_OFF, LOCALCONFIG_ON]) is True
    assert steamsetup.h264_unlocked([LOCALCONFIG_OFF]) is False
    assert steamsetup.h264_unlocked([]) is False


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.3.275", True), ("1.4.305", True), ("1.3", True), ("1.2.170", False), ("1.1", False)],
)
def test_the_vulkan_version_decides_whether_the_loader_is_new_enough(version, expected):
    assert steamsetup.vulkan_at_least_1_3(version) is expected


@pytest.mark.parametrize("version", [None, "", "garbage", "Vulkan"])
def test_an_unknown_vulkan_version_is_not_treated_as_old(version):
    assert steamsetup.vulkan_at_least_1_3(version) is None


# ---- the ladder --------------------------------------------------------------------------
def test_a_game_that_is_not_installed_starts_with_an_install_step(tmp_path):
    plan = steamsetup.plan_setup(UMA, steamapps_dirs=[tmp_path / "none"])
    install = _item(plan, "installed")
    assert install.state == "todo" and install.action == "steam_install"
    # Nothing after it can be judged yet.
    assert all(item.state == "blocked" for item in plan.items if item.key != "installed")
    assert not plan.ready


def test_video_game_needs_the_h264_unlock_until_it_is_on(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    off = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_OFF])
    item = _item(off, "h264")
    assert item.state == "todo" and item.action == "unlock_h264"
    assert not item.needs_steam_closed and item.warning != ""  # it asks first
    assert not off.ready
    on = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON])
    assert _item(on, "h264").state == "done"
    assert on.ready


def test_a_game_without_video_trouble_has_no_h264_step(tmp_path):
    _library(tmp_path, EPIC)
    plan = _plan(EPIC, tmp_path, localconfig_texts=[LOCALCONFIG_OFF])
    assert [item.key for item in plan.items if item.key == "h264"] == []


def test_prefix_steps_follow_the_row_and_report_what_is_missing(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    plan = _plan(EPIC, tmp_path)
    prefix = _item(plan, "prefix:webview2")
    assert prefix.state == "todo" and prefix.action == "install_prefix:webview2"
    assert not plan.ready
    _webview2(steamapps, EPIC)
    assert _item(_plan(EPIC, tmp_path), "prefix:webview2").state == "done"
    assert _plan(EPIC, tmp_path).ready


def test_a_missing_prefix_asks_for_a_first_run(tmp_path):
    _library(tmp_path, EPIC, pfx=False)
    plan = _plan(EPIC, tmp_path)
    first = _item(plan, "prefix:webview2")
    assert first.state == "blocked"
    assert "once" in first.detail.lower()


def test_the_runner_is_only_ever_optional_or_recommended(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    modern = _item(_plan(UMA, tmp_path, vulkan_version="1.4.305"), "runner")
    assert modern.state == "optional" and modern.action == "set_runner"
    old = _item(_plan(UMA, tmp_path, vulkan_version="1.2.170"), "runner")
    assert old.state == "recommended"
    unknown = _item(_plan(UMA, tmp_path, vulkan_version=None), "runner")
    assert unknown.state == "optional"
    # Neither blocks "ready": the game works on Proton Experimental when Vulkan is new enough.


def test_old_vulkan_makes_the_runner_part_of_being_ready(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    _webview2(steamapps, EPIC)
    old = _plan(EPIC, tmp_path, vulkan_version="1.2.170")
    assert not old.ready  # recommended and not applied yet
    modern = _plan(EPIC, tmp_path, vulkan_version="1.4.305")
    assert modern.ready


def test_the_runner_change_warns_and_needs_steam_closed(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    item = _item(_plan(UMA, tmp_path, vulkan_version="1.2.170"), "runner")
    assert item.needs_steam_closed
    warning = item.warning.lower()
    assert "compatibility" in warning and "vulkan" in warning
    assert "undo" in warning or "restore" in warning or "revert" in warning


def test_the_runner_is_done_when_steam_already_uses_it(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    done = _plan(
        UMA, tmp_path, vulkan_version="1.2.170", localconfig_texts=[LOCALCONFIG_ON],
        compat_text=COMPAT_TEXT % (UMA, "dwproton-11.0-15"),
    )
    assert _item(done, "runner").state == "done"
    assert done.ready
    other = _plan(
        UMA, tmp_path, vulkan_version="1.2.170",
        compat_text=COMPAT_TEXT % (UMA, "proton_experimental"),
    )
    assert _item(other, "runner").state == "recommended"
    for_other_game = _plan(
        UMA, tmp_path, vulkan_version="1.2.170",
        compat_text=COMPAT_TEXT % (EPIC, "dwproton-11.0-15"),
    )
    assert _item(for_other_game, "runner").state == "recommended"


def test_the_ladder_order_is_install_h264_prefix_runner(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    plan = _plan(UMA, tmp_path)
    keys = [item.key for item in plan.items]
    assert keys == ["installed", "h264", "runner"]
    steamapps = _library(tmp_path, EPIC)
    keys = [item.key for item in _plan(EPIC, tmp_path).items]
    assert keys == ["installed", "prefix:webview2", "runner"]
    assert steamapps.is_dir()


def test_an_unknown_game_is_refused_and_an_unverified_one_is_labelled(tmp_path):
    with pytest.raises(BackendError, match="no known"):
        _plan("999", tmp_path)
    _library(tmp_path, "3347400")
    plan = _plan("3347400", tmp_path)
    assert plan.verified is False and "region" in plan.note.lower()


def test_the_next_step_is_the_first_todo_item(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    plan = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_OFF])
    assert plan.next_item.key == "h264"
    done = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON])
    assert done.next_item is None


def test_steam_must_be_running_for_h264_and_closed_for_the_runner(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    plan = _plan(UMA, tmp_path, vulkan_version="1.2.170", localconfig_texts=[LOCALCONFIG_OFF])
    assert _item(plan, "h264").needs_steam_running
    assert not _item(plan, "runner").needs_steam_running


# ---- every step that changes something asks first --------------------------------------------
def test_the_h264_unlock_explains_itself_before_it_runs(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    item = _item(_plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_OFF]), "h264")
    text = item.warning.lower()
    assert "h.264" in text and "steam" in text
    assert "account" in text  # it is a setting of the Steam account, not of this game only


def test_the_prefix_install_names_the_backup_and_the_running_game(tmp_path):
    _library(tmp_path, EPIC)
    item = _item(_plan(EPIC, tmp_path), "prefix:webview2")
    text = item.warning.lower()
    assert "back up" in text and "close the game" in text
    assert "webview2" in text


def test_steps_that_only_ask_steam_need_no_extra_confirmation(tmp_path):
    plan = steamsetup.plan_setup(UMA, steamapps_dirs=[tmp_path / "none"])
    assert _item(plan, "installed").warning == ""  # Steam shows its own install dialog


def test_a_finished_step_carries_no_warning(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    _webview2(steamapps, EPIC)
    plan = _plan(EPIC, tmp_path, localconfig_texts=[LOCALCONFIG_ON])
    assert _item(plan, "prefix:webview2").warning == ""


# ---- every step that can run says what pressing its button does ------------------------------
def test_each_runnable_step_names_its_action_in_the_imperative(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    h264 = _item(_plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_OFF]), "h264")
    assert h264.action_label == "Unlock H.264 video"
    runner = _item(_plan(UMA, tmp_path), "runner")
    assert runner.action_label == "Use DW-Proton"
    steamapps = _library(tmp_path, EPIC)
    assert steamapps.is_dir()
    prefix = _item(_plan(EPIC, tmp_path), "prefix:webview2")
    assert prefix.action_label == "Install Microsoft WebView2"
    missing = steamsetup.plan_setup(UMA, steamapps_dirs=[tmp_path / "none"])
    assert _item(missing, "installed").action_label == "Install in Steam"


def test_a_step_without_an_action_has_no_action_label(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    _webview2(steamapps, EPIC)
    plan = _plan(EPIC, tmp_path)
    assert _item(plan, "prefix:webview2").action_label == ""  # done
    assert _item(plan, "installed").action_label == ""


# ---- scrutiny fixes ----------------------------------------------------------------------------
def test_the_next_step_is_the_recommended_runner_when_nothing_else_is_left(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    _webview2(steamapps, EPIC)
    plan = _plan(EPIC, tmp_path, vulkan_version="1.2.170")
    assert not plan.ready
    assert plan.next_item is not None and plan.next_item.key == "runner"
    optional = _plan(EPIC, tmp_path, vulkan_version="1.4.305")
    assert optional.next_item is None  # an optional runner is never pushed


def test_a_todo_step_still_comes_before_a_recommended_runner(tmp_path):
    _library(tmp_path, EPIC)
    plan = _plan(EPIC, tmp_path, vulkan_version="1.2.170")
    assert plan.next_item.key == "prefix:webview2"


def test_a_prefix_step_waits_for_the_game_to_be_installed(tmp_path):
    plan = steamsetup.plan_setup(EPIC, steamapps_dirs=[tmp_path / "none"])
    prefix = _item(plan, "prefix:webview2")
    assert prefix.state == "blocked" and "Install the game" in prefix.detail
    assert prefix.action == ""


def test_the_runner_warnings_promise_only_what_the_code_does(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    for version in ("1.2.170", "1.4.305"):
        text = _item(_plan(UMA, tmp_path, vulkan_version=version), "runner").warning.lower()
        assert "at any time" not in text and "same screen" not in text
        assert "if it is running" in text or "if steam is running" in text
        # how to undo it, truthfully: the button also works in a later window (it is kept on disk)
        assert "undo" in text and "open it again later" in text and "properties" in text


def test_the_old_vulkan_warning_names_the_older_vulkan_build_dw_proton_switches_to(tmp_path):
    _library(tmp_path, UMA, pfx=False)
    old = _item(_plan(UMA, tmp_path, vulkan_version="1.2.170"), "runner").warning
    assert "Sarek" in old and "older" in old


def test_blue_archives_note_says_what_was_actually_tested():
    note = steamsetup.game_for(BLUE).note.lower()
    assert "h.264" in note  # the unlock was already on in the test
    assert "no changes" not in note


def test_only_the_haoplay_build_of_gfl2_needs_webview2():
    haoplay = steamsetup.game_for("3308670")
    darkwinter = steamsetup.game_for("3347400")
    assert haoplay.needs == (steamfix.KEY_WEBVIEW2,)  # its login SDK embeds WebView2
    assert darkwinter.needs == ()  # the owner reports its sign-in does not use WebView2


# ---- the native Wayland step ------------------------------------------------------------
def _wayland_item(plan):
    return next((item for item in plan.items if item.key == "wayland"), None)


def test_there_is_no_wayland_step_on_an_x11_session(tmp_path):
    _library(tmp_path, UMA)
    plan = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=False)
    assert _wayland_item(plan) is None


def test_a_wayland_session_with_a_capable_runner_is_offered_native_wayland(tmp_path):
    _library(tmp_path, UMA)
    plan = _plan(
        UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=True,
        runner_has_wayland=True,
    )
    item = _wayland_item(plan)
    assert item.state == steamsetup.STATE_OPTIONAL
    assert item.action == "set_wayland" and item.needs_steam_closed
    assert item.action_label == "Use native Wayland"
    # the side effects other tools warn about (ProtonPlus docs): overlay and controllers
    for word in ("overlay", "controller"):
        assert word in item.warning.lower()
    for word in ("launch option", "PROTON_ENABLE_WAYLAND=1", "undo", "Steam"):
        assert word.lower() in item.warning.lower()
    assert plan.ready  # an optional step never blocks "ready"


def test_a_runner_without_the_wayland_driver_is_told_so_and_offers_no_button(tmp_path):
    _library(tmp_path, UMA)
    plan = _plan(
        UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=True,
        runner_has_wayland=False,
    )
    item = _wayland_item(plan)
    assert item.state == steamsetup.STATE_OPTIONAL and item.action == ""
    assert "DW-Proton" in item.detail and "Wayland driver" in item.detail
    assert plan.ready


def test_native_wayland_already_in_the_launch_options_is_done(tmp_path):
    _library(tmp_path, UMA)
    plan = _plan(
        UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=True,
        runner_has_wayland=False, launch_options="PROTON_ENABLE_WAYLAND=1 %command%",
    )
    item = _wayland_item(plan)
    assert item.state == steamsetup.STATE_DONE and item.action == ""


def test_the_wayland_step_waits_for_the_game_to_be_installed(tmp_path):
    plan = _plan(UMA, tmp_path, wayland_session=True)
    assert _wayland_item(plan).state == steamsetup.STATE_BLOCKED


def test_the_wayland_step_comes_after_the_runner_step(tmp_path):
    _library(tmp_path, UMA)
    plan = _plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=True)
    keys = [item.key for item in plan.items]
    assert keys.index("wayland") == keys.index("runner") + 1


# ---- Haoplay's sign-in needs GE-Proton, not DW-Proton ------------------------------------------
HAOPLAY = "3308670"


def _compat(app: str, tool: str) -> str:
    return COMPAT_TEXT % (app, tool)


def test_each_game_names_the_runner_family_that_suits_it():
    assert steamsetup.game_for(HAOPLAY).runner == "ge"
    for other in (EPIC, UMA, BLUE):
        assert steamsetup.game_for(other).runner == "dwproton"


def test_haoplay_is_offered_ge_proton_and_says_why(tmp_path):
    steamapps = _library(tmp_path, HAOPLAY)
    _webview2(steamapps, HAOPLAY)
    runner = _item(_plan(HAOPLAY, tmp_path, vulkan_version="1.4.3"), "runner")
    assert runner.label == "GE-Proton runner" and runner.action == "set_runner"
    assert runner.state == steamsetup.STATE_RECOMMENDED  # its sign-in does not work without it
    assert runner.action_label == "Use GE-Proton"
    text = (runner.detail + " " + runner.warning).lower()
    for word in ("sign-in", "ge-proton", "wayland", "dw-proton", "undo"):
        assert word in text
    assert "dxvk-sarek" not in text  # that reason belongs to the old-Vulkan DW-Proton step


def test_haoplay_on_ge_proton_is_done_but_on_dw_proton_it_is_still_to_do(tmp_path):
    steamapps = _library(tmp_path, HAOPLAY)
    _webview2(steamapps, HAOPLAY)
    ok = _plan(HAOPLAY, tmp_path, compat_text=_compat(HAOPLAY, "GE-Proton11-7-x86_64"))
    assert _item(ok, "runner").state == steamsetup.STATE_DONE
    assert "GE-Proton11-7-x86_64" in _item(ok, "runner").detail
    wrong = _plan(HAOPLAY, tmp_path, compat_text=_compat(HAOPLAY, "dwproton-11.0-15-x86_64"))
    assert _item(wrong, "runner").state == steamsetup.STATE_RECOMMENDED
    assert _item(wrong, "runner").action == "set_runner"


def test_the_other_games_keep_the_dw_proton_step_as_before(tmp_path):
    steamapps = _library(tmp_path, EPIC)
    _webview2(steamapps, EPIC)
    runner = _item(_plan(EPIC, tmp_path, vulkan_version="1.4.3"), "runner")
    assert runner.label == "DW-Proton runner" and runner.state == steamsetup.STATE_OPTIONAL
    assert runner.action_label == "Use DW-Proton"
    done = _plan(EPIC, tmp_path, compat_text=_compat(EPIC, "dwproton-11.0-15-x86_64"))
    assert _item(done, "runner").state == steamsetup.STATE_DONE


def test_a_game_that_is_not_installed_blocks_its_runner_step_under_its_own_name(tmp_path):
    assert _item(_plan(HAOPLAY, tmp_path), "runner").label == "GE-Proton runner"
    assert _item(_plan(HAOPLAY, tmp_path), "runner").state == steamsetup.STATE_BLOCKED


def test_haoplay_asks_for_umu_id_zero_with_native_wayland_other_games_for_nothing_extra():
    assert steamsetup.game_for("3308670").launch_env == ("UMU_ID=0",)
    for other in (EPIC, UMA, BLUE):
        assert steamsetup.game_for(other).launch_env == ()


def test_the_wayland_warning_names_the_extra_variable_of_that_game_only(tmp_path):
    _library(tmp_path, "3308670")
    _webview2(tmp_path / "lib" / "steamapps", "3308670")
    kwargs = dict(wayland_session=True, runner_has_wayland=True)
    haoplay = _item(_plan("3308670", tmp_path, **kwargs), "wayland")
    assert "UMU_ID=0" in haoplay.warning
    _library(tmp_path, UMA)
    uma = _item(_plan(UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], **kwargs), "wayland")
    assert "UMU_ID" not in uma.warning


# ---- done means the game's own variables too (#970) ------------------------------------------
def test_native_wayland_is_not_done_while_the_games_own_variable_is_missing(tmp_path):
    _library(tmp_path, "3308670")
    kwargs = dict(wayland_session=True, runner_has_wayland=True)
    missing = _wayland_item(
        _plan("3308670", tmp_path, launch_options="PROTON_ENABLE_WAYLAND=1 %command%", **kwargs)
    )
    assert missing.state != steamsetup.STATE_DONE and missing.action == "set_wayland"
    assert "UMU_ID=0" in missing.detail
    for options in (
        "UMU_ID=0 PROTON_ENABLE_WAYLAND=1 %command%",
        "UMU_ID=7 PROTON_ENABLE_WAYLAND=1 %command%",  # the player's own value stays
    ):
        done = _wayland_item(_plan("3308670", tmp_path, launch_options=options, **kwargs))
        assert done.state == steamsetup.STATE_DONE and done.action == ""


def test_a_game_with_no_extra_variables_is_done_as_before(tmp_path):
    _library(tmp_path, UMA)
    done = _wayland_item(
        _plan(
            UMA, tmp_path, localconfig_texts=[LOCALCONFIG_ON], wayland_session=True,
            runner_has_wayland=True, launch_options="PROTON_ENABLE_WAYLAND=1 %command%",
        )
    )
    assert done.state == steamsetup.STATE_DONE
