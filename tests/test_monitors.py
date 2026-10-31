# SPDX-License-Identifier: GPL-3.0-or-later
"""Connected monitors from /sys/class/drm and the monitor launch variable (#779)."""

from __future__ import annotations

import logging

import pytest

from nadare.core import monitors

VAR = "WAYLANDDRV_PRIMARY_MONITOR"


def _drm(root, **connectors):
    """Build a fake /sys/class/drm: ``name`` -> (status, enabled, modes)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "version").write_text("drm 1.1.0 20060810\n")
    (root / "renderD128").mkdir(exist_ok=True)
    (root / "card0").mkdir(exist_ok=True)
    for entry, (status, enabled, modes) in connectors.items():
        d = root / entry
        d.mkdir()
        (d / "status").write_text(status + "\n")
        (d / "enabled").write_text(enabled + "\n")
        (d / "modes").write_text(modes)
    return root


def _mainpc(tmp_path):
    return _drm(
        tmp_path / "drm",
        **{
            "card0-DP-1": ("connected", "enabled", "2560x1440\n2560x1440\n"),
            "card0-DP-2": ("connected", "enabled", "1920x1080\n"),
            "card0-DP-3": ("disconnected", "disabled", ""),
            "card0-HDMI-A-1": ("disconnected", "disabled", ""),
            "card0-Writeback-1": ("unknown", "disabled", ""),
            "card0-Virtual-1": ("connected", "enabled", "1024x768\n"),
        },
    )


def test_only_connected_real_connectors_are_listed(tmp_path):
    found = monitors.connected(_mainpc(tmp_path))
    assert [m.name for m in found] == ["DP-1", "DP-2"]


def test_the_first_mode_is_the_label_size(tmp_path):
    found = monitors.connected(_mainpc(tmp_path))
    assert [m.size for m in found] == ["2560x1440", "1920x1080"]
    assert found[0].label == "DP-1 (2560x1440)"


def test_a_switched_off_monitor_is_listed_but_flagged(tmp_path):
    root = _drm(tmp_path / "drm", **{"card0-HDMI-A-1": ("connected", "disabled", "1920x1080\n")})
    (monitor,) = monitors.connected(root)
    assert monitor.enabled is False
    assert "off" in monitor.label


def test_a_laptop_panel_is_just_another_connector(tmp_path):
    root = _drm(
        tmp_path / "drm",
        **{
            "card0-eDP-1": ("connected", "enabled", "1920x1200\n"),
            "card0-HDMI-A-1": ("connected", "enabled", "3840x2160\n"),
        },
    )
    assert [m.name for m in monitors.connected(root)] == ["HDMI-A-1", "eDP-1"]


def test_two_gpus_list_each_monitor_once_even_with_a_shared_name(tmp_path):
    root = _drm(
        tmp_path / "drm",
        **{
            "card0-DP-1": ("connected", "enabled", "3840x2160\n"),
            "card1-DP-1": ("connected", "enabled", "1920x1080\n"),
            "card1-DP-2": ("connected", "enabled", "1920x1080\n"),
        },
    )
    found = monitors.connected(root)
    assert [m.name for m in found] == ["DP-1", "DP-2"]
    assert found[0].size == "3840x2160"  # the first card keeps a name two cards share


def test_the_list_is_ordered_by_name_not_by_card(tmp_path):
    root = _drm(
        tmp_path / "drm",
        **{
            "card0-HDMI-A-1": ("connected", "enabled", "1920x1080\n"),
            "card1-DP-1": ("connected", "enabled", "1920x1080\n"),
        },
    )
    assert [m.name for m in monitors.connected(root)] == ["DP-1", "HDMI-A-1"]


def test_a_headless_server_has_no_monitors(tmp_path):
    root = _drm(tmp_path / "drm", **{"card0-DP-1": ("disconnected", "disabled", "")})
    assert monitors.connected(root) == []


def test_a_missing_or_unreadable_drm_folder_is_an_empty_list(tmp_path):
    assert monitors.connected(tmp_path / "nope") == []
    root = _drm(tmp_path / "drm", **{"card0-DP-1": ("connected", "enabled", "")})
    (root / "card0-DP-2").mkdir()  # no status file at all
    assert [m.name for m in monitors.connected(root)] == ["DP-1"]


def test_a_monitor_without_modes_still_lists(tmp_path):
    root = _drm(tmp_path / "drm", **{"card0-DP-1": ("connected", "enabled", "")})
    (monitor,) = monitors.connected(root)
    assert monitor.label == "DP-1"


# ---- the launch variable ------------------------------------------------


def test_a_connected_monitor_sets_the_variable(tmp_path):
    root = _mainpc(tmp_path)
    assert monitors.launch_env({"monitor": "DP-2"}, {}, root) == {VAR: "DP-2"}


def test_auto_or_unset_sets_nothing(tmp_path):
    root = _mainpc(tmp_path)
    assert monitors.launch_env({}, {}, root) == {}
    assert monitors.launch_env({"monitor": ""}, {}, root) == {}
    assert monitors.launch_env({"monitor": "auto"}, {}, root) == {}


def test_the_global_default_applies_when_the_game_has_none(tmp_path):
    root = _mainpc(tmp_path)
    config = {"default_monitor": "DP-1"}
    assert monitors.launch_env({}, config, root) == {VAR: "DP-1"}
    assert monitors.launch_env({"monitor": "auto"}, config, root) == {VAR: "DP-1"}


def test_the_games_own_choice_beats_the_global_default(tmp_path):
    root = _mainpc(tmp_path)
    assert monitors.launch_env({"monitor": "DP-2"}, {"default_monitor": "DP-1"}, root) == {
        VAR: "DP-2"
    }


def test_a_monitor_that_is_not_connected_is_dropped_with_a_log_line(tmp_path, caplog):
    # An unknown name would make Wine open the game on the left-most monitor, so drop it.
    root = _mainpc(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert monitors.launch_env({"monitor": "DP-3"}, {}, root) == {}
    assert "DP-3" in caplog.text


def test_an_unlistable_machine_keeps_the_choice(tmp_path):
    # Flatpak or an odd sandbox where /sys/class/drm is unreadable: trust the user's name.
    assert monitors.launch_env({"monitor": "DP-2"}, {}, tmp_path / "nope") == {VAR: "DP-2"}


@pytest.mark.parametrize("bad", [5, None, ["DP-1"], {"a": 1}, "DP-1 --evil", "a=b", "x\ny"])
def test_nonsense_values_are_ignored(tmp_path, bad):
    root = _mainpc(tmp_path)
    assert monitors.launch_env({"monitor": bad}, {}, root) == {}
    assert monitors.launch_env({}, {"default_monitor": bad}, root) == {}
