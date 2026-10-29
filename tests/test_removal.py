# SPDX-License-Identifier: GPL-3.0-or-later
"""Removing a game from the library (core/removal.py and ``nadare remove``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare import app
from nadare.core import paths, removal, shortcuts
from nadare.core.backends import BackendError, Status
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg_path = tmp_path / "config.json"
    cfg = Config.load(cfg_path)
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()
    return cfg, cfg_path, tmp_path


def test_remove_game_drops_profile_and_persists(env):
    cfg, cfg_path, tmp = env
    result = removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")
    assert result.profile == "gone"
    assert "gone" not in cfg.profiles and "keep" in cfg.profiles
    assert cfg.active == "keep"
    assert "gone" not in Config.load(cfg_path).profiles  # saved to disk


def test_remove_game_removes_only_its_shortcuts(env):
    cfg, _, tmp = env
    apps = tmp / "apps"
    shortcuts.install_desktop_entry("gone", "Gone", applications_dir=apps, argv=["nadare"])
    shortcuts.install_desktop_entry("keep", "Keep", applications_dir=apps, argv=["nadare"])
    result = removal.remove_game(cfg, "gone", applications_dir=apps)
    assert [p.name for p in result.shortcuts_removed] == ["nadare-gone.desktop"]
    assert (apps / "nadare-keep.desktop").exists()
    assert not (apps / "nadare-gone.desktop").exists()


def test_remove_game_keeps_data_by_default(env):
    cfg, _, tmp = env
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    (data / "save.dat").write_text("x")
    result = removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")
    assert data.exists() and result.data_dir_removed is None


def test_remove_game_delete_data_removes_only_the_profile_folder(env):
    cfg, _, tmp = env
    own = paths.profiles_dir() / "gone"
    other = paths.profiles_dir() / "keep"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    (own / "a").write_text("x")
    (other / "b").write_text("x")
    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    assert result.data_dir_removed == own
    assert not own.exists() and (other / "b").exists()


def test_delete_data_never_follows_a_symlink_out_of_the_profiles_dir(env):
    cfg, _, tmp = env
    outside = tmp / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    paths.profiles_dir().mkdir(parents=True)
    (paths.profiles_dir() / "gone").symlink_to(outside, target_is_directory=True)
    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    assert (outside / "keep.txt").exists()
    assert result.data_dir_removed is None
    assert any("symlink" in w.lower() for w in result.warnings)


def test_delete_data_leaves_an_install_dir_outside_the_profiles_dir(env):
    cfg, _, tmp = env
    install = tmp / "Games" / "gone"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg.profiles["gone"]["install_dir"] = str(install)
    removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp / "apps")
    assert (install / "game.exe").exists()


def test_unknown_profile_raises(env):
    cfg, _, tmp = env
    with pytest.raises(BackendError):
        removal.remove_game(cfg, "nope", applications_dir=tmp / "apps")


def test_saved_login_failure_is_a_warning_not_an_error(env, monkeypatch):
    cfg, _, tmp = env
    calls = []

    class Boom:
        supports_remember_me = True

        def __init__(self, ctx):
            pass

        def status(self):
            return Status("installed")

        @staticmethod
        def forget_saved_login(profile):
            calls.append(profile)
            raise RuntimeError("keyring locked")

    monkeypatch.setattr(removal.backends, "get", lambda kind: Boom)
    result = removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")
    assert calls == ["gone"]
    assert "gone" not in cfg.profiles
    assert any("keyring locked" in w for w in result.warnings)


def test_cli_remove_requires_explicit_profile_and_yes(env, capsys):
    cfg, cfg_path, _ = env
    assert app.run_cli(["-c", str(cfg_path), "remove"]) != 0
    assert "gone" in Config.load(cfg_path).profiles
    # --yes missing: nothing removed
    assert app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone"]) != 0
    assert "gone" in Config.load(cfg_path).profiles


def test_cli_remove_with_yes(env, capsys):
    cfg, cfg_path, _ = env
    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "gone" not in Config.load(cfg_path).profiles
    assert "removed gone" in out.lower()


def test_cli_remove_delete_data_flag(env):
    cfg, cfg_path, _ = env
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-data"])
    assert code == 0 and not data.exists()


def test_drop_profile_false_leaves_the_config_untouched(env):
    cfg, cfg_path, tmp = env
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    result = removal.remove_game(
        cfg, "gone", delete_data=True, drop_profile=False, applications_dir=tmp / "apps"
    )
    assert result.data_dir_removed == data
    assert "gone" in cfg.profiles
    assert "gone" in Config.load(cfg_path).profiles


# ---- opt-in deletion of the install dir / prefix --------------------------------
def _tree(tmp_path, *parts):
    p = tmp_path.joinpath(*parts)
    p.mkdir(parents=True)
    (p / "file.bin").write_text("x")
    return p


@pytest.fixture
def trees_env(env, monkeypatch):
    cfg, cfg_path, tmp = env
    monkeypatch.setenv("HOME", str(tmp / "home"))
    (tmp / "home").mkdir(exist_ok=True)
    return cfg, cfg_path, tmp


def test_removable_trees_lists_existing_configured_dirs(trees_env):
    cfg, _, tmp = trees_env
    install = _tree(tmp, "Games", "gone")
    prefix = _tree(tmp, "Prefixes", "gone")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    cfg.profiles["gone"]["extra"] = {}
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert set(found) == {"install_dir", "prefix"}
    assert found["install_dir"].safe and found["prefix"].safe
    assert found["install_dir"].path == install.resolve()


def test_missing_or_empty_paths_are_not_listed(trees_env):
    cfg, _, tmp = trees_env
    cfg.profiles["gone"].update(install_dir=str(tmp / "nowhere"), prefix="")
    assert removal.removable_trees(cfg, "gone") == []


def test_delete_trees_removes_only_the_requested_keys(trees_env):
    cfg, _, tmp = trees_env
    install = _tree(tmp, "Games", "gone")
    prefix = _tree(tmp, "Prefixes", "gone")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    result = removal.remove_game(
        cfg, "gone", delete_trees=["prefix"], applications_dir=tmp / "apps"
    )
    assert not prefix.exists() and install.exists()
    assert result.trees_removed == [prefix.resolve()]


def test_nothing_is_deleted_without_delete_trees(trees_env):
    cfg, _, tmp = trees_env
    install = _tree(tmp, "Games", "gone")
    cfg.profiles["gone"]["install_dir"] = str(install)
    removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")
    assert install.exists()


def test_wineprefix_key_is_handled_too(trees_env):
    cfg, _, tmp = trees_env
    wp = _tree(tmp, "wp", "gone")
    cfg.profiles["gone"].update(wineprefix=str(wp), owned_trees=["wineprefix"])
    result = removal.remove_game(
        cfg, "gone", delete_trees=["wineprefix"], applications_dir=tmp / "apps"
    )
    assert not wp.exists() and result.trees_removed == [wp.resolve()]


def _refused(cfg, tmp, key, expect_word):
    result = removal.remove_game(cfg, "gone", delete_trees=[key], applications_dir=tmp / "apps")
    assert result.trees_removed == []
    assert any(expect_word in w.lower() for w in result.warnings), result.warnings


def test_a_dir_shared_with_another_profile_is_refused(trees_env):
    cfg, _, tmp = trees_env
    shared = _tree(tmp, "Games", "shared")
    cfg.profiles["gone"]["install_dir"] = str(shared)
    cfg.profiles["keep"]["install_dir"] = str(shared)
    _refused(cfg, tmp, "install_dir", "keep")
    assert shared.exists()


def test_a_dir_that_contains_another_profiles_dir_is_refused(trees_env):
    cfg, _, tmp = trees_env
    parent = _tree(tmp, "Games")
    child = _tree(tmp, "Games", "keep-game")
    cfg.profiles["gone"]["install_dir"] = str(parent)
    cfg.profiles["keep"]["install_dir"] = str(child)
    _refused(cfg, tmp, "install_dir", "keep")
    assert child.exists()


def test_home_and_its_ancestors_are_refused(trees_env):
    cfg, _, tmp = trees_env
    home = tmp / "home"
    cfg.profiles["gone"]["install_dir"] = str(home)
    _refused(cfg, tmp, "install_dir", "home")
    cfg.profiles["gone"]["install_dir"] = str(tmp)
    _refused(cfg, tmp, "install_dir", "home")


def test_a_mount_point_is_refused(trees_env, monkeypatch):
    cfg, _, tmp = trees_env
    mount = _tree(tmp, "mnt", "disk")
    cfg.profiles["gone"]["install_dir"] = str(mount)
    real = removal.os.path.ismount
    monkeypatch.setattr(
        removal.os.path, "ismount", lambda p: str(p) == str(mount.resolve()) or real(p)
    )
    _refused(cfg, tmp, "install_dir", "mount")
    assert mount.exists()


def test_a_symlinked_dir_is_refused_and_target_survives(trees_env):
    cfg, _, tmp = trees_env
    target = _tree(tmp, "real")
    link = tmp / "Games" / "gone"
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    cfg.profiles["gone"]["install_dir"] = str(link)
    _refused(cfg, tmp, "install_dir", "symlink")
    assert (target / "file.bin").exists()


def test_nadares_own_dirs_are_never_deleted(trees_env):
    cfg, _, tmp = trees_env
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    cfg.profiles["gone"]["install_dir"] = str(paths.data_dir().parent)
    _refused(cfg, tmp, "install_dir", "nadare")
    assert paths.data_dir().exists()


def test_cli_delete_install_dir_and_prefix_flags(trees_env):
    cfg, cfg_path, tmp = trees_env
    install = _tree(tmp, "Games", "gone")
    prefix = _tree(tmp, "Prefixes", "gone")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["install_dir", "prefix"]
    )
    cfg.save()
    code = app.run_cli(
        ["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-install-dir"]
    )
    assert code == 0 and not install.exists() and prefix.exists()
    cfg = Config.load(cfg_path)
    cfg.add_profile("gone", backend="wine", prefix=str(prefix), owned_trees=["prefix"])
    cfg.save()
    code = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-prefix"])
    assert code == 0 and not prefix.exists()


# --- round-1 audit fixes -----------------------------------------------------


@pytest.mark.parametrize(
    "where",
    ["profiles", "runners", "logs", "cache_x", "config_x", "keep"],
)
def test_descendants_of_nadare_dirs_are_unsafe(trees_env, where):
    cfg, _, tmp = trees_env
    target = {
        "profiles": paths.profiles_dir(),
        "runners": paths.runners_dir(),
        "logs": paths.logs_dir(),
        "cache_x": paths.cache_dir() / "x",
        "config_x": paths.config_dir() / "x",
        "keep": paths.profiles_dir() / "keep",
    }[where]
    target.mkdir(parents=True, exist_ok=True)
    cfg.profiles["gone"]["install_dir"] = str(target)
    (tree,) = removal.removable_trees(cfg, "gone")
    assert tree.safe is False
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert target.exists() and "gone" in cfg.profiles and result.profile_kept


def test_own_profile_folder_prefix_is_deletable(trees_env):
    cfg, _, tmp = trees_env
    target = paths.profiles_dir() / "gone" / "prefix"
    target.mkdir(parents=True)
    cfg.profiles["gone"]["prefix"] = str(target)
    (tree,) = removal.removable_trees(cfg, "gone")
    assert tree.safe
    removal.remove_game(cfg, "gone", delete_trees=["prefix"], applications_dir=tmp / "apps")
    assert not target.exists()


def test_other_profiles_folder_is_refused(trees_env):
    cfg, _, tmp = trees_env
    target = paths.profiles_dir() / "keep" / "prefix"
    target.mkdir(parents=True)
    cfg.profiles["gone"]["prefix"] = str(target)
    (tree,) = removal.removable_trees(cfg, "gone")
    assert tree.safe is False


def test_cli_remove_refused_tree_exits_nonzero(trees_env, capsys):
    cfg, cfg_path, tmp = trees_env
    cfg.profiles["gone"]["install_dir"] = str(Path.home())
    cfg.save()
    rc = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-install-dir"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "removed gone" not in out
    assert "kept" in out.lower()


def test_duplicate_prefix_deleted_once(trees_env, capsys):
    cfg, cfg_path, tmp = trees_env
    p = _tree(tmp, "Prefixes", "gone")
    cfg.profiles["gone"].update(
        prefix=str(p), wineprefix=str(p), owned_trees=["prefix", "wineprefix"]
    )
    cfg.save()
    rc = app.run_cli(["-c", str(cfg_path), "remove", "-p", "gone", "--yes", "--delete-prefix"])
    out = capsys.readouterr().out
    assert rc == 0 and "warning" not in out and not p.exists()
    assert "gone" not in Config.load(cfg_path).profiles


def test_uncreatable_xdg_dir_gives_clean_error(tmp_path, monkeypatch, capsys):
    f = tmp_path / "file"
    f.write_text("x")
    monkeypatch.setenv("XDG_DATA_HOME", str(f))
    assert app.run_cli(["-c", str(tmp_path / "c.json"), "list"]) == 1
    assert capsys.readouterr().err.strip()


def _mk(tmp, *parts):
    p = tmp.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    (p / "f").write_text("x")
    return p


def test_nested_prefix_inside_install_dir_is_deleted_once(env):
    cfg, _, tmp = env
    inst = _mk(tmp, "games", "g")
    pfx = _mk(tmp, "games", "g", "pfx")
    cfg.profiles["gone"].update(
        install_dir=str(inst), prefix=str(pfx), owned_trees=["install_dir", "prefix"]
    )
    for order in (["install_dir", "prefix"], ["prefix", "install_dir"]):
        result = removal.remove_game(
            cfg, "gone", delete_trees=order, applications_dir=tmp / "apps", save=False
        )
        assert result.warnings == []
        assert not result.profile_kept
        assert not inst.exists()
        assert result.trees_removed == [inst]
        cfg.add_profile("gone", backend="wine", title="Gone")
        _mk(tmp, "games", "g", "pfx")
        cfg.profiles["gone"].update(
            install_dir=str(inst), prefix=str(pfx), owned_trees=["install_dir", "prefix"]
        )


def test_removable_trees_sees_extra_paths_of_other_profile(env):
    cfg, _, tmp = env
    shared = _mk(tmp, "shared")
    cfg.profiles["gone"]["install_dir"] = str(shared)
    cfg.profiles["keep"]["extra"] = {"install_dir": str(shared)}
    trees = removal.removable_trees(cfg, "gone")
    assert [t.safe for t in trees] == [False]
    assert "keep" in trees[0].reason


def test_removable_trees_lists_extra_only_prefix(env):
    cfg, _, tmp = env
    pfx = _mk(tmp, "pfx")
    cfg.profiles["gone"]["extra"] = {"prefix": str(pfx)}
    cfg.profiles["gone"]["owned_trees"] = ["prefix"]
    trees = removal.removable_trees(cfg, "gone")
    assert [(t.key, t.safe) for t in trees] == [("prefix", True)]


def test_delete_data_refused_when_other_profile_prefix_inside(env):
    cfg, _, tmp = env
    pfx = paths.profiles_dir() / "gone" / "pfx"
    pfx.mkdir(parents=True)
    (pfx / "f").write_text("x")
    cfg.profiles["keep"]["prefix"] = str(pfx)
    result = removal.remove_game(
        cfg, "gone", delete_data=True, applications_dir=tmp / "apps", save=False
    )
    assert (pfx / "f").exists()
    assert result.data_dir_removed is None
    assert any("keep" in w for w in result.warnings)


def test_a_refused_tree_keeps_the_profile_but_deletes_safe_trees(env, monkeypatch):
    cfg, _, tmp = env
    apps = tmp / "apps"
    shortcuts.install_desktop_entry("gone", "Gone", applications_dir=apps, argv=["nadare"])
    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    (data / "save").write_text("x")
    shared = _mk(tmp, "shared")
    ok = _mk(tmp, "ok")
    cfg.profiles["gone"]["install_dir"] = str(shared)
    cfg.profiles["gone"]["prefix"] = str(ok)
    cfg.profiles["gone"]["owned_trees"] = ["prefix"]
    cfg.profiles["keep"]["install_dir"] = str(shared)

    class Boom:
        supports_remember_me = True

        def __init__(self, ctx):
            pass

        def status(self):
            return Status("installed")

        @staticmethod
        def forget_saved_login(profile):
            raise AssertionError("login must not be forgotten")

    monkeypatch.setattr(removal.backends, "get", lambda kind: Boom)
    result = removal.remove_game(
        cfg,
        "gone",
        delete_data=True,
        delete_trees=["install_dir", "prefix"],
        applications_dir=apps,
        save=False,
    )
    assert result.profile_kept
    assert result.warnings
    assert (apps / "nadare-gone.desktop").exists()
    assert (data / "save").exists()
    assert result.trees_removed == [ok.resolve()]
    assert not (ok / "f").exists() and (shared / "f").exists()
    assert "gone" in cfg.profiles


# ---- only folders Nadare created may be deleted ----------------------------------
def test_a_folder_nadare_did_not_create_is_refused_even_with_one_game_in_it(trees_env):
    cfg, _, tmp = trees_env
    library = _tree(tmp, "Games")  # a user folder that happens to hold only this game
    cfg.profiles["gone"]["install_dir"] = str(library)
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert not found["install_dir"].safe
    assert "did not create" in found["install_dir"].reason
    _refused(cfg, tmp, "install_dir", "did not create")
    assert (library / "file.bin").exists()


def test_a_folder_listed_as_created_by_nadare_may_be_deleted(trees_env):
    cfg, _, tmp = trees_env
    made = _tree(tmp, "Games", "Punishing Gray Raven")
    cfg.profiles["gone"].update(install_dir=str(made), owned_trees=["install_dir"])
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert found["install_dir"].safe
    result = removal.remove_game(
        cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps"
    )
    assert not made.exists() and result.trees_removed == [made.resolve()]


def test_ownership_is_per_key(trees_env):
    cfg, _, tmp = trees_env
    install = _tree(tmp, "Games", "gone")
    prefix = _tree(tmp, "Games", "gone-prefix")
    cfg.profiles["gone"].update(
        install_dir=str(install), prefix=str(prefix), owned_trees=["prefix"]
    )
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert found["prefix"].safe and not found["install_dir"].safe


def test_a_wine_prefix_nadare_did_not_create_is_refused_even_with_drive_c(trees_env):
    cfg, _, tmp = trees_env
    shared = _tree(tmp, "home", ".wine")
    (shared / "drive_c").mkdir()
    cfg.profiles["gone"]["prefix"] = str(shared)
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert not found["prefix"].safe and "did not create" in found["prefix"].reason
    _refused(cfg, tmp, "prefix", "did not create")
    assert (shared / "file.bin").exists()


def test_a_recorded_path_that_was_changed_later_is_no_longer_owned(trees_env):
    cfg, _, tmp = trees_env
    made = _tree(tmp, "Games", "made")
    later = _tree(tmp, "Music")
    cfg.profiles["gone"].update(
        install_dir=str(later),
        owned_trees=["install_dir"],
        owned_paths={"install_dir": str(made.resolve())},
    )
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert not found["install_dir"].safe
    _refused(cfg, tmp, "install_dir", "location changed")
    assert (later / "file.bin").exists()


def test_a_recorded_path_that_still_matches_is_owned(trees_env):
    cfg, _, tmp = trees_env
    made = _tree(tmp, "Games", "made")
    cfg.profiles["gone"].update(
        install_dir=str(made),
        owned_trees=["install_dir"],
        owned_paths={"install_dir": str(made.resolve())},
    )
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    assert found["install_dir"].safe


def test_a_prefix_field_pointing_at_a_plain_folder_is_refused(trees_env):
    cfg, _, tmp = trees_env
    plain = _tree(tmp, "Documents")
    cfg.profiles["gone"]["prefix"] = str(plain)
    _refused(cfg, tmp, "prefix", "did not create")
    assert plain.exists()


def test_removing_both_trees_also_tidies_the_empty_game_folder(trees_env):
    cfg, _, tmp = trees_env
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    prefix = _tree(tmp, "Games", "Nadare", "Gone", "prefix")
    cfg.profiles["gone"].update(
        install_dir=str(install),
        prefix=str(prefix),
        owned_trees=["install_dir", "prefix"],
        owned_dirs=[str(game_dir)],
    )
    removal.remove_game(
        cfg, "gone", delete_trees=["install_dir", "prefix"], applications_dir=tmp / "apps"
    )
    assert not game_dir.exists()
    assert not (tmp / "Games" / "Nadare").exists()  # the empty wrapper parent is tidied too


def test_a_game_folder_that_still_holds_something_is_left_alone(trees_env):
    cfg, _, tmp = trees_env
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    install = _tree(tmp, "Games", "Nadare", "Gone", "install")
    (game_dir / "my-saves.txt").write_text("keep me")
    cfg.profiles["gone"].update(
        install_dir=str(install), owned_trees=["install_dir"], owned_dirs=[str(game_dir)]
    )
    removal.remove_game(cfg, "gone", delete_trees=["install_dir"], applications_dir=tmp / "apps")
    assert not install.exists()
    assert (game_dir / "my-saves.txt").read_text() == "keep me"


def test_an_owned_dir_is_not_removed_when_no_tree_was_deleted(trees_env):
    cfg, _, tmp = trees_env
    game_dir = tmp / "Games" / "Nadare" / "Gone"
    game_dir.mkdir(parents=True)
    cfg.profiles["gone"]["owned_dirs"] = [str(game_dir)]
    removal.remove_game(cfg, "gone", applications_dir=tmp / "apps")
    assert game_dir.is_dir()
