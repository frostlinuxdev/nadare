# SPDX-License-Identifier: GPL-3.0-or-later
"""Removal cleanup: wrapper parents, running games, partial refusal and reasons."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import paths, process, removal
from nadare.core.backends import Status
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


def _tree(root: Path, *parts: str) -> Path:
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    (p / "file.bin").write_text("x")
    return p


def _backend(monkeypatch, *, state: str = "installed", error: Exception | None = None) -> None:
    class Fake:
        def status(self) -> Status:
            if error is not None:
                raise error
            return Status(state)

    monkeypatch.setattr(removal.backends, "create", lambda *a, **k: Fake())


# ---- 1. the empty Nadare wrapper parent goes too ---------------------------------
def test_the_empty_nadare_parent_is_removed_with_the_game_folder(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    prefix = _tree(tmp, "Games", "Nadare", "Gone", "prefix")
    cfg.profiles["gone"].update(
        install_dir=str(install),
        prefix=str(prefix),
        owned_trees=["install_dir", "prefix"],
        owned_dirs=[str(game_dir)],
    )
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert not result.profile_kept
    assert not game_dir.exists()
    assert not (tmp / "Games" / "Nadare").exists()
    assert (tmp / "Games").is_dir()


def test_a_nadare_parent_that_still_holds_another_game_is_kept(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    _tree(tmp, "Games", "Nadare", "Other", "install")
    cfg.profiles["gone"].update(
        install_dir=str(install), owned_trees=["install_dir"], owned_dirs=[str(game_dir)]
    )
    removal.remove_game(cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps")
    assert not game_dir.exists()
    assert (tmp / "Games" / "Nadare" / "Other" / "install").is_dir()


def test_a_parent_that_is_not_called_nadare_is_kept(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    game_dir = tmp / "Games" / "Wrapper" / "Gone"
    install = _tree(tmp, "Games", "Wrapper", "Gone", "install")
    cfg.profiles["gone"].update(
        install_dir=str(install), owned_trees=["install_dir"], owned_dirs=[str(game_dir)]
    )
    removal.remove_game(cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps")
    assert not game_dir.exists()
    assert (tmp / "Games" / "Wrapper").is_dir()


# ---- 2. never remove a game that is running (or whose state is unknown) -----------
def test_a_running_game_is_refused_before_anything_is_deleted(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch, state="running")
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert result.profile_kept and result.trees_removed == []
    assert "gone" in cfg.profiles
    assert (install / "file.bin").exists()
    assert any("running" in w.lower() for w in result.warnings), result.warnings


def test_an_unknown_state_refuses_with_a_could_not_check_message(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch, error=RuntimeError("process scan failed"))
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert result.profile_kept and result.trees_removed == []
    assert "gone" in cfg.profiles and (install / "file.bin").exists()
    assert any("could not check" in w.lower() for w in result.warnings), result.warnings


def test_a_held_profile_lock_refuses_before_anything_is_deleted(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    with process.ProfileLock("gone"):
        result = removal.remove_game(
            cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
        )
    assert result.profile_kept and result.trees_removed == []
    assert "gone" in cfg.profiles and (install / "file.bin").exists()
    assert result.warnings


# ---- 3. one bad tree must not cancel the safe ones -------------------------------
def test_an_unsafe_tree_does_not_stop_a_safe_tree_from_being_deleted(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    shared = _tree(tmp, "Games", "shared")
    safe = _tree(tmp, "Games", "safe")
    cfg.profiles["gone"].update(
        install_dir=str(safe), prefix=str(shared), owned_trees=["install_dir", "prefix"]
    )
    cfg.profiles["keep"]["install_dir"] = str(shared)
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert result.trees_removed == [safe.resolve()]
    assert not safe.exists() and (shared / "file.bin").exists()
    assert result.profile_kept and "gone" in cfg.profiles
    assert any("prefix" in w and "keep" in w for w in result.warnings), result.warnings


def test_a_missing_tree_does_not_stop_an_existing_safe_tree(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    prefix = _tree(tmp, "Prefixes", "gone")
    cfg.profiles["gone"].update(
        install_dir=str(tmp / "nowhere"),
        prefix=str(prefix),
        owned_trees=["install_dir", "prefix"],
    )
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert result.trees_removed == [prefix.resolve()]
    assert not prefix.exists()
    assert result.profile_kept and "gone" in cfg.profiles
    assert any("install folder" in w for w in result.warnings), result.warnings


# ---- 4. data_dir_removed only when the folder really went ------------------------
def test_delete_data_reports_nothing_removed_when_game_files_are_kept(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    data = paths.profiles_dir() / "gone"
    install = _tree(data, "install")
    (data / "save.dat").write_text("x")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    assert data.is_dir() and (install / "file.bin").exists()
    assert result.data_dir_removed is None
    assert any("kept the game files" in w for w in result.warnings), result.warnings


# ---- 5. a moved folder is not "a folder Nadare did not create" -------------------
def test_a_relinked_folder_says_its_location_changed_instead(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    made = _tree(tmp, "Games", "made")
    later = _tree(tmp, "Music", "moved")
    cfg.profiles["gone"].update(
        prefix=str(later),
        owned_trees=["prefix"],
        owned_paths={"prefix": str(made.resolve())},
    )
    (tree,) = removal.removable_trees(cfg, "gone")
    assert not tree.safe
    assert "location changed" in tree.reason.lower()
    assert "kept" in tree.reason.lower()
    assert "did not create" not in tree.reason.lower()
    result = removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )
    assert (later / "file.bin").exists()
    assert any("location changed" in w.lower() for w in result.warnings), result.warnings


def test_a_home_folder_named_nadare_is_never_removed(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    home = tmp / "Nadare"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    game_dir = home / "Gone"
    install = _tree(home, "Gone", "install")
    cfg.profiles["gone"].update(
        install_dir=str(install), owned_trees=["install_dir"], owned_dirs=[str(game_dir)]
    )
    removal.remove_game(cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps")
    assert not game_dir.exists()
    assert home.is_dir()


def test_a_profile_whose_backend_cannot_be_built_can_still_be_removed(env, monkeypatch):
    """No loadable backend means nothing of Nadare's can be running, so removal must not stall."""
    from nadare.core.backends import BackendError

    cfg, tmp = env

    def broken(*_a, **_k):
        raise BackendError("backend not available in this build")

    monkeypatch.setattr(removal.backends, "create", broken)
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert not result.profile_kept
    assert result.trees_removed and not install.exists()
    assert "gone" not in cfg.profiles


# ---- 6. a tree that fails at run time must not destroy the rest -------------------
def test_a_failing_tree_keeps_shortcuts_login_and_data(env, monkeypatch):
    """The pre-check promise ("kept ⇒ nothing else changed") holds for a runtime failure too."""
    cfg, tmp = env
    forgotten: list[str] = []

    class Fake:
        supports_remember_me = True

        def status(self) -> Status:
            return Status("installed")

        @staticmethod
        def forget_saved_login(profile: str) -> None:
            forgotten.append(profile)

    monkeypatch.setattr(removal.backends, "create", lambda *a, **k: Fake())
    monkeypatch.setattr(removal.backends, "get", lambda kind: Fake)
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    (data / "settings.json").write_text("{}")
    apps = tmp / "applications"
    removal.shortcuts.install_desktop_entry(
        "gone", "Gone", applications_dir=apps, argv=["nadare"]
    )
    entry = apps / "nadare-gone.desktop"

    real_rmtree = removal.shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path) == install.resolve():
            raise OSError("disk went away")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(removal.shutil, "rmtree", failing_rmtree)
    result = removal.remove_game(
        cfg, "gone", delete_data=True, delete_trees=["install_dir"], applications_dir=apps
    )
    assert result.profile_kept and "gone" in cfg.profiles
    assert any("could not remove install_dir" in w for w in result.warnings), result.warnings
    assert install.is_dir()
    assert entry.exists(), "a shortcut was removed although the profile was kept"
    assert data.is_dir() and result.data_dir_removed is None
    assert not result.login_forgotten and forgotten == []


# ---- 7. the game can start between the probe and the first deletion --------------
def test_a_game_starting_after_the_first_probe_stops_the_deletion(env, monkeypatch):
    cfg, tmp = env
    states = iter(["installed", "running"])

    class Fake:
        def status(self) -> Status:
            return Status(next(states, "running"))

    monkeypatch.setattr(removal.backends, "create", lambda *a, **k: Fake())
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"].update(install_dir=str(install), owned_trees=["install_dir"])
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert result.profile_kept and result.trees_removed == []
    assert "gone" in cfg.profiles and (install / "file.bin").exists()
    assert any("running" in w.lower() for w in result.warnings), result.warnings


# ---- 8. install_dir itself is the wrapper (Steam / FGO style) --------------------
def test_the_wrapper_goes_when_the_install_dir_is_the_wrapper(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    library = tmp / "Games"
    game_dir = library / "Nadare" / "Gone"
    _tree(game_dir, "drive_c")
    cfg.profiles["gone"].update(
        install_dir=str(game_dir), owned_trees=["install_dir"], owned_dirs=[str(game_dir)]
    )
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert not result.profile_kept and not result.warnings
    assert not game_dir.exists()
    assert not (library / "Nadare").exists()
    assert library.is_dir()


# ---- a profile older Nadare made has no owned_dirs, but its folders look the same ----------------
def test_a_legacy_layout_loses_its_empty_wrapper_too(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    prefix = _tree(tmp, "Games", "Nadare", "Gone", "prefix")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert not result.profile_kept
    assert not (tmp / "Games" / "Nadare").exists()
    assert (tmp / "Games").is_dir()


def test_a_legacy_wrapper_with_a_user_file_is_kept(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    prefix = _tree(tmp, "Games", "Nadare", "Gone", "prefix")
    (tmp / "Games" / "Nadare" / "Gone" / "notes.txt").write_text("mine")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert (tmp / "Games" / "Nadare" / "Gone" / "notes.txt").read_text() == "mine"


def test_folders_the_user_laid_out_themselves_are_not_tidied(env, monkeypatch):
    cfg, tmp = env
    _backend(monkeypatch)
    install = _tree(tmp, "Games", "Nadare", "Gone", "game")  # not the Add Game name
    prefix = _tree(tmp, "Games", "Nadare", "Gone", "prefix")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert (tmp / "Games" / "Nadare" / "Gone").is_dir()
