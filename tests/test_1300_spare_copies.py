# SPDX-License-Identifier: GPL-3.0-or-later
"""#1300 item 5: after the player picks one copy of a game, the other copies can go.

Only folders Nadare made (the copy's manifest says so) are offered, install and prefix
separately; never one another game uses, never a prefix a program runs in; every check runs
again right before deleting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import gamefinder, libraries, removal
from nadare.core.config import Config


def _copy_folder(base: Path, folder: str, made=("install", "prefix"), backend="direct-hsr"):
    real = base / folder
    (real / "install").mkdir(parents=True)
    (real / "install" / "StarRail.exe").write_text("exe")
    (real / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (real / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": backend,
                "title": "Honkai: Star Rail",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": list(made),
            }
        )
    )
    return real.resolve()


@pytest.fixture
def case(tmp_path, monkeypatch):
    """HSR's profile points at a folder that is gone; two copies Nadare made are on the drive."""
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    monkeypatch.setattr(gamefinder, "_prefix_in_use", lambda prefix: False)
    base = tmp_path / "drive" / "Gacha games" / "Nadare"
    keep = _copy_folder(base, "Honkai Star Rail")
    spare = _copy_folder(base, "Honkai Star Rail old")
    old = base / "Nadare" / "Honkai Star Rail"
    config = Config.load(tmp_path / "config.json")
    config.data[libraries.CONFIG_KEY] = [{"path": str(base)}]
    config.add_profile(
        "hsr",
        backend="direct-hsr",
        title="Honkai: Star Rail",
        install_dir=str(old / "install"),
        prefix=str(old / "prefix"),
        owned_dirs=[str(old)],
        owned_trees=["install_dir", "prefix"],
    )
    config.save()
    return config, keep, spare


def _use(config, keep):
    copies = gamefinder.find_copies(config, "hsr", None)
    chosen = next(c for c in copies if c.game_dir == keep)
    gamefinder.use_and_save(config, "hsr", chosen)
    return copies, chosen


def _one(config, copies, chosen, key):
    return next(s for s in gamefinder.spares(config, "hsr", copies, chosen) if s.key == key)


def _paths(spares) -> set[Path]:
    return {s.path for s in spares}


def test_the_other_copys_install_and_prefix_are_offered_separately(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    spares = gamefinder.spares(config, "hsr", copies, chosen)
    assert _paths(spares) == {spare / "install", spare / "prefix"}
    assert {s.key for s in spares} == {"install_dir", "prefix"}
    assert all(s.game_dir == spare for s in spares)


def test_the_chosen_copy_is_never_offered(case):
    config, keep, _spare = case
    copies, chosen = _use(config, keep)
    for item in gamefinder.spares(config, "hsr", copies, chosen):
        assert keep not in item.path.parents and item.path != keep


def test_only_what_nadare_made_is_offered(case, tmp_path):
    config, keep, spare = case
    base = spare.parent
    partly = _copy_folder(base, "Honkai Star Rail partly", made=("install",))
    _copy_folder(base, "Honkai Star Rail own", made=())
    copies, chosen = _use(config, keep)
    offered = _paths(gamefinder.spares(config, "hsr", copies, chosen))
    assert partly / "install" in offered
    assert partly / "prefix" not in offered
    assert not any("own" in p.parent.name for p in offered)


def test_a_copy_without_a_manifest_is_never_offered(case):
    config, keep, spare = case
    (spare / libraries.MANIFEST_NAME).unlink()
    copies, chosen = _use(config, keep)
    assert gamefinder.spares(config, "hsr", copies, chosen) == []


def test_a_folder_another_game_uses_is_never_offered(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    config.add_profile("other", backend="wine", prefix=str(spare / "prefix"))
    assert _paths(gamefinder.spares(config, "hsr", copies, chosen)) == {spare / "install"}


def test_a_prefix_a_program_runs_in_is_never_offered(case, monkeypatch):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    monkeypatch.setattr(gamefinder, "_prefix_in_use", lambda prefix: prefix == spare / "prefix")
    assert _paths(gamefinder.spares(config, "hsr", copies, chosen)) == {spare / "install"}


def test_a_linked_tree_is_never_offered(case, tmp_path):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    outside = tmp_path / "outside"
    outside.mkdir()
    import shutil

    shutil.rmtree(spare / "install")
    (spare / "install").symlink_to(outside)
    # The manifest's install now points outside its folder: the whole copy is refused.
    assert gamefinder.spares(config, "hsr", copies, chosen) == []
    assert outside.is_dir()


# ---- removing -----------------------------------------------------------------------------------
def test_removing_both_trees_removes_the_copy_folder(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    for item in gamefinder.spares(config, "hsr", copies, chosen):
        assert gamefinder.remove_spare(config, "hsr", item, chosen) == ""
    assert not spare.exists()
    assert (keep / "install" / "StarRail.exe").is_file()


def test_removing_one_tree_keeps_the_rest(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "prefix")
    assert gamefinder.remove_spare(config, "hsr", item, chosen) == ""
    assert not (spare / "prefix").exists()
    assert (spare / "install" / "StarRail.exe").is_file()
    assert (spare / libraries.MANIFEST_NAME).is_file()


def test_remove_rechecks_a_game_added_meanwhile(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "prefix")
    config.add_profile("other", backend="wine", prefix=str(spare / "prefix"))
    assert gamefinder.remove_spare(config, "hsr", item, chosen) != ""
    assert (spare / "prefix" / "drive_c").is_dir()


def test_remove_rechecks_the_manifest(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "install_dir")
    data = json.loads((spare / libraries.MANIFEST_NAME).read_text())
    data["made_by_nadare"] = ["prefix"]  # the player's own files now
    (spare / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    assert gamefinder.remove_spare(config, "hsr", item, chosen) != ""
    assert (spare / "install" / "StarRail.exe").is_file()


def test_remove_rechecks_a_running_prefix(case, monkeypatch):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "prefix")
    monkeypatch.setattr(gamefinder, "_prefix_in_use", lambda prefix: True)
    assert gamefinder.remove_spare(config, "hsr", item, chosen) != ""
    assert (spare / "prefix" / "drive_c").is_dir()


def test_remove_refuses_the_chosen_copy(case):
    config, keep, _spare = case
    _copies, chosen = _use(config, keep)
    forged = gamefinder.Spare(game_dir=keep, key="install_dir", path=keep / "install", size=0)
    assert gamefinder.remove_spare(config, "hsr", forged, chosen) != ""
    assert (keep / "install" / "StarRail.exe").is_file()


def test_remove_refuses_a_path_the_manifest_does_not_name(case, tmp_path):
    config, keep, spare = case
    _copies, chosen = _use(config, keep)
    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    (elsewhere / "thesis.odt").write_text("keep")
    forged = gamefinder.Spare(game_dir=spare, key="install_dir", path=elsewhere, size=0)
    assert gamefinder.remove_spare(config, "hsr", forged, chosen) != ""
    assert (elsewhere / "thesis.odt").is_file()


def test_remove_never_crosses_a_mount_inside(case, monkeypatch):
    # The careful remover is the one deletion path (it keeps mounted drives inside).
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "prefix")
    used = []
    real = removal._rmtree_safely
    monkeypatch.setattr(
        removal, "_rmtree_safely", lambda root, keep=(): used.append(root) or real(root)
    )
    gamefinder.remove_spare(config, "hsr", item, chosen)
    assert used == [spare / "prefix"]


def test_a_measured_size_is_given(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "install_dir")
    assert item.size == len("exe")


def test_remove_rechecks_which_game_the_folder_belongs_to(case):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    item = _one(config, copies, chosen, "install_dir")
    data = json.loads((spare / libraries.MANIFEST_NAME).read_text())
    data["backend"] = "direct-genshin"  # now another game's folder
    (spare / libraries.MANIFEST_NAME).write_text(json.dumps(data))
    assert gamefinder.remove_spare(config, "hsr", item, chosen) != ""
    assert (spare / "install" / "StarRail.exe").is_file()


def test_the_list_never_shows_a_folder_that_would_be_refused(case, monkeypatch):
    config, keep, spare = case
    copies, chosen = _use(config, keep)
    monkeypatch.setattr(gamefinder, "_prefix_in_use", lambda prefix: True)
    assert [s.key for s in gamefinder.spares(config, "hsr", copies, chosen)] == ["install_dir"]


def test_the_running_check_reads_the_real_process_table(tmp_path, monkeypatch):
    from nadare.core import process

    prefix = tmp_path / "prefix"
    seen = []
    monkeypatch.setattr(process, "prefix_pids", lambda p: seen.append(p) or [4242])
    assert gamefinder._prefix_in_use(prefix) is True
    monkeypatch.setattr(process, "prefix_pids", lambda p: [])
    assert gamefinder._prefix_in_use(prefix) is False
    assert seen == [prefix]
