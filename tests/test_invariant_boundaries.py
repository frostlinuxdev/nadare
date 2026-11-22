# SPDX-License-Identifier: GPL-3.0-or-later
"""Agreement between stored settings, folder readers and destructive boundaries."""

from __future__ import annotations

import pytest

from nadare.core import gamefinder, gamefolders, libraries, location, paths, relink
from nadare.core.config import Config


@pytest.mark.parametrize("key", ["prefix", "install_dir"])
def test_extra_only_external_game_is_not_claimed_as_default_folder(tmp_path, key):
    external = tmp_path / "imported"
    external.mkdir()
    profile = {"backend": "direct-bd2", "extra": {key: str(external)}}
    assert location.game_folder(profile, "g") is None
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", **profile)
    place = gamefolders.placement(cfg, "g")
    assert place is None or not place.made_by_nadare


@pytest.mark.parametrize("key", ["prefix", "install_dir"])
def test_missing_folder_in_extra_is_reported_as_missing(tmp_path, key):
    profile = {"backend": "direct-bd2", "extra": {key: str(tmp_path / "missing")}}
    assert key in gamefinder.folders_missing(profile)


def test_manifest_uses_the_same_extra_paths_as_launch(tmp_path):
    game = tmp_path / "Nadare" / "Game"
    (game / "install").mkdir(parents=True)
    profile = {
        "backend": "direct-bd2",
        "title": "Game",
        "extra": {"install_dir": str(game / "install")},
        "owned_trees": [],
    }
    assert libraries.write_manifest(profile, game) is not None
    assert libraries.read_manifest(game).install_dir == game / "install"


def test_new_move_destination_skips_unowned_partial_folder(tmp_path):
    root = tmp_path / "Nadare"
    partial = root / "Game.nadare-partial"
    partial.mkdir(parents=True)
    (partial / "keep").write_bytes(b"user")
    assert relink.free_folder(root, "Game", {}) != root / "Game"
    assert (partial / "keep").read_bytes() == b"user"


def test_steam_shape_stays_outside_managed_storage():
    profile = {"backend": "steam", "steam_app_id": "123"}
    assert location.game_folder(profile, "steam") is None
    assert libraries.write_manifest(profile, paths.profiles_dir() / "steam") is None


def test_legacy_backend_uninstall_is_not_a_gui_worker_operation(tmp_path):
    from nadare.gui.workers import OPERATIONS, BackendWorker

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine")
    assert "uninstall" not in OPERATIONS
    with pytest.raises(ValueError):
        BackendWorker(cfg, "g", "uninstall")


def test_extra_folder_is_visible_in_placement_and_manifest_upkeep(tmp_path):
    install = tmp_path / "Imported" / "install"
    install.mkdir(parents=True)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="direct-bd2", extra={"install_dir": str(install)})
    assert gamefolders.placement(cfg, "g").folder == install
    assert libraries._has_game_folders(cfg.profiles["g"])


@pytest.mark.parametrize(
    "value,expected",
    [
        (float("inf"), 4),
        (float("-inf"), 4),
        (float("nan"), 4),
        (10**100, 16),
        (-(10**100), 1),
        ("nonsense", 4),
    ],
)
def test_integer_setting_editor_handles_the_same_bad_numbers_as_queue(qtbot, value, expected):
    from PyQt6.QtWidgets import QSpinBox

    from nadare.core.backends import Option
    from nadare.gui.settings import SettingsPage

    editor = QSpinBox()
    editor.setRange(1, 16)
    page = SettingsPage()
    try:
        page._set_editor(Option("download_workers", "Workers", "int", default=4), editor, value)
        assert editor.value() == expected
    finally:
        page.deleteLater()
        editor.deleteLater()
        qtbot.wait(1)


def test_manifest_comparison_uses_same_extra_folder_as_writer(tmp_path):
    game = tmp_path / "Nadare" / "Game"
    (game / "install").mkdir(parents=True)
    profile = {
        "backend": "direct-bd2",
        "title": "Game",
        "extra": {"install_dir": str(game / "install")},
    }
    libraries.write_manifest(profile, game)
    assert libraries._matches(libraries.read_manifest(game), profile)


def test_move_refuses_a_retargeted_destination_library_alias(tmp_path):
    from nadare.core import relocate

    old, first, second = tmp_path / "old", tmp_path / "first", tmp_path / "second"
    for folder in (old, first, second):
        folder.mkdir()
    (old / "game").write_bytes(b"keep")
    alias = tmp_path / "library"
    alias.symlink_to(first, target_is_directory=True)
    destination = alias / "Game"
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, destination)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    with pytest.raises(relocate.RelocateError):
        relocate.move_tree(old, destination, journal=profile[relocate.MOVE_IN_PROGRESS])
    assert (old / "game").read_bytes() == b"keep"
    assert not list(first.iterdir()) and not list(second.iterdir())


def test_overlong_single_instance_name_preserves_existing_regular_file(tmp_path, qtbot):
    from nadare.gui.single_instance import SingleInstance

    parent = tmp_path / ("long-directory-" * 8)
    parent.mkdir()
    socket = parent / "s.sock"
    socket.write_bytes(b"keep this regular file")
    instance = SingleInstance(str(socket))
    try:
        assert not instance.start()
        assert socket.read_bytes() == b"keep this regular file"
    finally:
        instance.close()


def test_recovery_keeps_record_if_destination_anchor_is_a_symlink_loop(tmp_path):
    from nadare.core import relocate

    old, destination = tmp_path / "old", tmp_path / "destination"
    old.mkdir()
    profile = {"prefix": str(old)}
    relocate.begin_move(profile, old, destination)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    profile[relocate.MOVE_IN_PROGRESS]["anchor"] = str(loop)
    assert relocate.finish_interrupted_moves({"g": profile})
    assert relocate.MOVE_IN_PROGRESS in profile


def test_reset_stray_clears_legacy_extra_value_too(tmp_path):
    game = tmp_path / "Nadare" / "Game"
    (game / "prefix").mkdir(parents=True)
    external = tmp_path / "import"
    external.mkdir()
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "g",
        backend="launcher-nikke",
        owned_dirs=[str(game)],
        owned_trees=["prefix"],
        extra={"prefix": str(game / "prefix"), "install_dir": str(external)},
    )
    assert gamefolders.placement(cfg, "g").stray.get("install_dir", "absent") is None
    gamefolders.reset_stray(cfg, "g")
    assert not gamefolders.placement(cfg, "g").stray
    assert location._setting(cfg.profiles["g"], "install_dir") is None


def test_prefix_move_reads_legacy_extra_folders(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for prefix in (old, new):
        (prefix / "drive_c" / "Game").mkdir(parents=True)
    profile = {
        "backend": "wine",
        "extra": {"prefix": str(old), "install_dir": str(old / "drive_c" / "Game")},
    }
    relink.check_movable(profile)
    plan = relink.plan_relink(profile, new)
    assert plan.changes["prefix"] == str(new)
    assert plan.changes["install_dir"] == str(new / "drive_c" / "Game")


def test_whole_move_plan_uses_the_same_legacy_extra_folders(tmp_path):
    old, new = tmp_path / "Nadare" / "Game", tmp_path / "Other" / "Nadare" / "Game"
    for home in (old, new):
        (home / "prefix" / "drive_c").mkdir(parents=True)
        (home / "install").mkdir()
    profile = {
        "backend": "direct-bd2",
        "extra": {"prefix": str(old / "prefix"), "install_dir": str(old / "install")},
        "owned_dirs": [str(old)],
        "owned_trees": ["prefix", "install_dir"],
    }
    assert relink.managed_home(profile) == old
    plan = relink.plan_home_move(profile, new)
    assert plan.changes["prefix"] == str(new / "prefix")
    assert plan.changes["install_dir"] == str(new / "install")


def test_simulated_worlds_exercise_legacy_extra_prefixes(tmp_path):
    from storage_sim import _env, build_world

    observed = 0
    for seed in range(20):
        base = tmp_path / str(seed)
        with _env(base):
            cfg, _, _ = build_world(seed, base)
            observed += sum(
                bool(p.get("extra", {}).get("prefix")) and not p.get("prefix")
                for p in cfg.profiles.values()
            )
    assert observed > 0, "world generator never exercises the legacy prefix path"


def test_simulator_isolates_desktop_services_and_restores_environment(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    from storage_sim import _env

    from nadare.core import secrets

    inherited = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/session-bus",
        "DISPLAY": ":99",
        "WAYLAND_DISPLAY": "synthetic-wayland",
        "XDG_RUNTIME_DIR": "/synthetic/runtime",
        "APPIMAGE": "/synthetic/appimage",
        "FLATPAK_ID": "synthetic.flatpak",
        "NADARE_NO_KEYRING": "",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    def forbidden():
        pytest.fail("simulator reached desktop keyring")

    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=forbidden))
    with _env(tmp_path):
        assert os.environ.get("NADARE_NO_KEYRING") == "1"
        assert secrets._keyring_module() is None
        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=" + str(
            tmp_path / "runtime" / "no-session-bus"
        )
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "APPIMAGE", "FLATPAK_ID"):
            assert key not in os.environ
        assert os.environ["XDG_RUNTIME_DIR"] == str(tmp_path / "runtime")
    for key, value in inherited.items():
        assert os.environ.get(key) == value
