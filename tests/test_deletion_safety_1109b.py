# SPDX-License-Identifier: GPL-3.0-or-later
"""Deletion safety, 2026-11-09 audit, batch B: removal, uninstall-all, per-game uninstall, config.

Reports in /mnt/kikyou/work/briefs/deletion-1109/ (w1, w3, w4).
"""

from __future__ import annotations

import json

import pytest

from nadare.core import paths, removal, uninstall_all
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    return cfg, tmp_path


def _default_prefix(name: str):
    """A game whose profile leaves ``prefix`` empty: the backend uses <data>/prefix."""
    prefix = paths.profiles_dir() / name / "prefix"
    save = prefix / "drive_c" / "users" / "steamuser" / "Saved Games" / "save.dat"
    save.parent.mkdir(parents=True)
    save.write_text("hours of play")
    return prefix, save


# ---------------------------------------------------------------------------
# w1 F1: "Nadare's data" must not take a default-located prefix with it
# ---------------------------------------------------------------------------
def test_deleting_nadares_data_keeps_a_prefix_in_its_default_place(env):
    cfg, tmp = env
    prefix, save = _default_prefix("gone")
    (paths.profiles_dir() / "gone" / "settings.json").write_text("{}")

    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")

    assert save.read_text() == "hours of play"
    assert not (paths.profiles_dir() / "gone" / "settings.json").exists()
    assert any("kept" in w for w in result.warnings)


def test_the_default_prefix_is_offered_as_a_tree_the_user_can_tick(env):
    cfg, _ = env
    prefix, _ = _default_prefix("gone")
    trees = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert "prefix" in trees
    assert trees["prefix"].path == prefix.resolve()
    assert trees["prefix"].safe  # Nadare made it: it lives in Nadare's own profile folder


def test_ticking_the_default_prefix_deletes_it(env):
    cfg, tmp = env
    prefix, _ = _default_prefix("gone")
    removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], delete_data=True, applications_dir=tmp / "apps"
    )
    assert not prefix.exists()


def test_uninstall_all_without_games_keeps_default_located_prefixes(tmp_path):
    for fn in (paths.data_dir, paths.config_dir, paths.state_dir, paths.cache_dir):
        fn().mkdir(parents=True, exist_ok=True)
    cfg = Config.load(paths.config_file())
    cfg.add_profile("arknights", backend="arknights-yostar", title="Arknights")
    cfg.save()
    prefix, save = _default_prefix("arknights")

    report = uninstall_all.run(cfg, applications_dir=tmp_path / "apps", desktop_dir=tmp_path / "d")

    assert not report.refused
    assert save.read_text() == "hours of play"
    groups = {g.key: g for g in uninstall_all.plan(Config.load(paths.config_file()))}
    assert groups  # the plan still works after the run


# ---------------------------------------------------------------------------
# w1 F2: deleting a ticked folder never takes an unticked nested one
# ---------------------------------------------------------------------------
def test_an_unticked_prefix_inside_the_ticked_game_folder_is_kept(env):
    cfg, tmp = env
    inst = tmp / "games" / "g"
    pfx = inst / "my-prefix"
    (pfx / "drive_c").mkdir(parents=True)
    (pfx / "drive_c" / "save.dat").write_text("mine")
    (inst / "game.exe").write_text("MZ")
    cfg.profiles["gone"].update(install_dir=str(inst), prefix=str(pfx), owned_trees=["install_dir"])

    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps", save=False
    )

    assert (pfx / "drive_c" / "save.dat").read_text() == "mine"
    assert not (inst / "game.exe").exists()
    assert result.profile_kept or any("kept" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# w4 1: a profile folder moved by an earlier run is still found
# ---------------------------------------------------------------------------
def test_a_profile_moved_by_an_earlier_run_that_did_not_save_points_at_the_new_folder(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    old_tree = paths.profiles_dir() / "a" / "b"
    (old_tree / "prefix").mkdir(parents=True)
    (old_tree / "prefix" / "marker").write_text("x")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"profiles": {"a/b": {"backend": "wine", "prefix": str(old_tree / "prefix")}}})
    )
    Config.load(path)  # moves the folder; this run never saves (nadare list, doctor)

    again = Config.load(path)
    moved = paths.profiles_dir() / "a-b" / "prefix"
    assert again.profiles["a-b"]["prefix"] == str(moved)
    assert (moved / "marker").exists()


# ---------------------------------------------------------------------------
# w3: per-game backends
# ---------------------------------------------------------------------------
def test_the_nikke_unpack_folder_is_never_looked_up_through_a_linked_user_folder(tmp_path):
    """users/<name> linked to the real home: the silent install would rmtree inside it."""
    from nadare.backends import launcher

    home = tmp_path / "home"
    (home / "AppData" / "Local" / "nikkeminiloader").mkdir(parents=True)
    users = tmp_path / "pfx" / "drive_c" / "users"
    users.mkdir(parents=True)
    (users / "frost").symlink_to(home)

    found = launcher._wine_user_dir(tmp_path / "pfx")

    assert not found.is_symlink()
    assert home.resolve() not in [found.resolve(), *found.resolve().parents]


def test_the_czn_prefix_marker_never_writes_through_a_link(tmp_path):
    from nadare.backends.czn.backend import PREFIX_MARKER, CznBackend

    outside = tmp_path / "important.txt"
    outside.write_text("keep me")
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    (prefix / PREFIX_MARKER).symlink_to(outside)
    (prefix / "czn-runner").symlink_to(outside)

    CznBackend._write_prefix_markers(prefix, "dwproton-11.0-15")

    assert outside.read_text() == "keep me"
    assert (prefix / PREFIX_MARKER).is_file() and not (prefix / PREFIX_MARKER).is_symlink()
    assert (prefix / "czn-runner").read_text().strip() == "dwproton-11.0-15"


def test_czn_uninstall_never_walks_into_a_mounted_drive(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.czn.backend import CznBackend

    backend = CznBackend(make_context(tmp_path, "stove-czn", {}))
    prefix = backend.prefix()
    (prefix / "drive_c").mkdir(parents=True)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    calls = []

    def spy(root, keep=()):
        calls.append(root)
        return removal._RmTreeResult([], [])

    monkeypatch.setattr(removal, "_rmtree_safely", spy)
    backend.uninstall(purge=True)
    assert prefix in calls


def test_czn_never_stamps_a_users_non_empty_folder_as_its_prefix(tmp_path, monkeypatch):
    """Sweep b23r1 c03: wineboot in a folder the user picked that already held files stamped
    it "created by Nadare", and uninstall then deleted the whole folder."""
    from _backend_helpers import make_context

    from nadare.backends.czn.backend import PREFIX_MARKER, CznBackend

    mine = tmp_path / "MyStuff"
    mine.mkdir()
    (mine / "notes.txt").write_text("mine")
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(mine)}))

    def fake_wineboot(self, args):
        (self.prefix() / "drive_c").mkdir(exist_ok=True)
        return 0

    monkeypatch.setattr(CznBackend, "_run_in_prefix", fake_wineboot)
    backend._ensure_prefix()
    assert not (mine / PREFIX_MARKER).exists()
    assert backend._prefix_not_deletable(mine) != ""
    backend.uninstall()
    assert (mine / "notes.txt").read_text() == "mine"


def test_czn_stamps_a_new_or_empty_prefix(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.czn.backend import PREFIX_MARKER, CznBackend

    for name, make in (("new", False), ("empty", True)):
        where = tmp_path / name / "prefix"
        if make:
            where.mkdir(parents=True)
        backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(where)}))
        monkeypatch.setattr(
            CznBackend,
            "_run_in_prefix",
            lambda self, args: (self.prefix() / "drive_c").mkdir(exist_ok=True) or 0,
        )
        backend._ensure_prefix()
        assert (where / PREFIX_MARKER).is_file()
