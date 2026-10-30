# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild backups: record the old prefix, offer it for removal, tidy failures."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.czn.backend import CznBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import prefixmove, removal
from nadare.core.config import Config

# ---------------------------------------------------------------------------
# 1. a successful rebuild records the backup
# ---------------------------------------------------------------------------


def _czn(tmp_path, **profile) -> CznBackend:
    values = {"prefix": str(tmp_path / "prefix"), **profile}
    return CznBackend(make_context(tmp_path, "stove-czn", values))


def test_czn_remake_records_the_backup_without_replacing_older_entries(tmp_path, monkeypatch):
    backend = _czn(tmp_path)
    old = str(tmp_path / "prefix.broken-20260101-000000")
    backend.ctx.profile["prefix_backups"] = [old]
    (backend.prefix() / "drive_c").mkdir(parents=True)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(
        backend, "_ensure_prefix", lambda: (backend.prefix() / "drive_c").mkdir(parents=True)
    )
    monkeypatch.setattr(backend, "install", lambda: None)

    backend.remake_prefix()

    (backup,) = list(tmp_path.glob("prefix.broken-*"))
    assert str(backup) != old
    assert backend.ctx.profile["prefix_backups"] == [old, str(backup.resolve())]


def _arknights(tmp_path, **profile) -> ArknightsBackend:
    return ArknightsBackend(make_context(tmp_path, ak.KIND, profile))


def test_arknights_remake_records_the_backup(tmp_path, monkeypatch):
    backend = _arknights(tmp_path)
    (backend.prefix() / "drive_c").mkdir(parents=True)
    monkeypatch.setattr(wg, "scan_pids", lambda patterns: [])
    monkeypatch.setattr(WineGameBackend, "_ensure_default_runner", lambda self, strict=True: None)
    monkeypatch.setattr(backend, "_ensure_prefix", lambda: True)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=True: None)

    backend.remake_prefix()

    (backup,) = list(backend.prefix().parent.glob("prefix.broken-*"))
    assert backend.ctx.profile["prefix_backups"] == [str(backup.resolve())]


# ---------------------------------------------------------------------------
# 2. removable_trees offers the recorded backup and refuses anything odd
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("gone", backend="wine", title="Gone")
    return cfg, tmp_path


def _profile_with_backup(cfg, prefix, *backups):
    cfg.profiles["gone"].update(prefix=str(prefix), prefix_backups=[str(p) for p in backups])


def _backup(root, prefix, suffix="20260102-030405"):
    path = root / f"{prefix.name}.broken-{suffix}"
    path.mkdir(parents=True)
    (path / "drive_c").mkdir()
    (path / "drive_c" / "stale.dll").write_text("old")
    return path


def test_removable_trees_lists_each_existing_backup_with_a_unique_key(env):
    cfg, tmp = env
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    first = _backup(tmp / "Games" / "Nadare" / "Gone", prefix)
    second = _backup(tmp / "Games" / "Nadare" / "Gone", prefix, "20260103-030405")
    _profile_with_backup(cfg, prefix, first, second)

    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}

    key1, key2 = f"prefix_backup:{first.name}", f"prefix_backup:{second.name}"
    assert found[key1].safe and found[key1].path == first.resolve()
    assert found[key2].safe and found[key2].path == second.resolve()


def test_a_properly_named_backup_elsewhere_is_still_deletable(env):
    """After a hand relink the prefix lives elsewhere but its old backups stay where they were."""
    cfg, tmp = env
    prefix = tmp / "Elsewhere" / "prefix"
    prefix.mkdir(parents=True)
    old_place = tmp / "Games" / "Nadare" / "Gone" / "prefix.broken-20260102-030405"
    old_place.mkdir(parents=True)
    _profile_with_backup(cfg, prefix, old_place)

    (tree,) = [t for t in removal.removable_trees(cfg, "gone") if t.key.startswith("prefix_backup")]
    assert tree.safe, tree.reason


def test_a_backup_with_a_foreign_name_is_refused(env):
    cfg, tmp = env
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    foreign = tmp / "Games" / "Nadare" / "Gone" / "important"
    foreign.mkdir()
    _profile_with_backup(cfg, prefix, foreign)

    (tree,) = [t for t in removal.removable_trees(cfg, "gone") if t.key.startswith("prefix_backup")]
    assert not tree.safe and "not named like an old prefix backup" in tree.reason
    assert foreign.is_dir()


def test_a_symlinked_backup_is_listed_but_never_deleted(env):
    cfg, tmp = env
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    target = tmp / "Somewhere"
    target.mkdir()
    link = tmp / "Games" / "Nadare" / "Gone" / "prefix.broken-20260102-030405"
    link.symlink_to(target, target_is_directory=True)
    _profile_with_backup(cfg, prefix, link)

    key = f"prefix_backup:{link.name}"
    found = {t.key: t for t in removal.removable_trees(cfg, "gone")}
    tree = found[key]
    assert not tree.safe and "symlink" in tree.reason
    result = removal.remove_game(cfg, "gone", delete_trees=[key], applications_dir=tmp / "apps")
    assert result.profile_kept
    assert link.is_symlink() and target.is_dir()
    assert result.trees_removed == []


def test_removing_a_recorded_backup_deletes_it_and_leaves_the_wrapper_removable(env):
    cfg, tmp = env
    wrapper = tmp / "Games" / "Nadare" / "Gone"
    prefix = wrapper / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(wrapper, prefix)
    _profile_with_backup(cfg, prefix, backup)
    cfg.profiles["gone"].update(
        owned_trees=["prefix"],
        owned_paths={"prefix": str(prefix.resolve())},
        owned_dirs=[str(wrapper)],
    )

    result = removal.remove_game(
        cfg,
        "gone",
        delete_trees=["prefix", f"prefix_backup:{backup.name}"],
        applications_dir=tmp / "apps",
    )

    assert not result.profile_kept
    assert not backup.exists()
    assert not wrapper.exists()


# ---------------------------------------------------------------------------
# 3. a failed rebuild does not leave an empty .failed-* folder behind
# ---------------------------------------------------------------------------


def test_roll_back_removes_a_parked_prefix_that_holds_only_empty_folders(tmp_path):
    prefix = tmp_path / "prefix"
    backup = tmp_path / "prefix.broken-20260102-030405"
    backup.mkdir()
    (backup / "old.txt").write_text("old")
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    (prefix / "drive_c" / "ProgramData").mkdir(parents=True)

    prefixmove.roll_back(backup, prefix)

    assert (prefix / "old.txt").read_text() == "old"
    assert not list(tmp_path.glob("prefix.failed-*"))


def test_roll_back_keeps_a_parked_prefix_that_holds_a_file(tmp_path):
    prefix = tmp_path / "prefix"
    backup = tmp_path / "prefix.broken-20260102-030405"
    backup.mkdir()
    (backup / "old.txt").write_text("old")
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "half-built.dll").write_text("half")

    prefixmove.roll_back(backup, prefix)

    (parked,) = list(tmp_path.glob("prefix.failed-*"))
    assert (parked / "drive_c" / "half-built.dll").read_text() == "half"


# ---------------------------------------------------------------------------
# 4. deleting one backup from Settings, through one core helper
# ---------------------------------------------------------------------------


def test_delete_backup_removes_the_folder_entry_and_saves(env):
    cfg, tmp = env
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(tmp / "Games" / "Nadare" / "Gone", prefix)
    _profile_with_backup(cfg, prefix, backup)

    deleted = prefixmove.delete_backup(cfg, "gone", backup)

    assert deleted == backup.resolve()
    assert not backup.exists()
    assert cfg.profiles["gone"]["prefix_backups"] == []
    assert Config.load(cfg.path).profiles["gone"]["prefix_backups"] == []


def test_delete_backup_refuses_a_path_that_is_not_recorded(env):
    cfg, tmp = env
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(tmp / "Games" / "Nadare" / "Gone", prefix)
    _profile_with_backup(cfg, prefix, tmp / "Games" / "Nadare" / "Gone" / "other")

    with pytest.raises(prefixmove.PrefixMoveError, match="not a recorded"):
        prefixmove.delete_backup(cfg, "gone", backup)

    assert backup.is_dir()


@pytest.fixture(scope="module")
def qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_settings_shows_the_backup_and_deletes_it_after_confirmation(env, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui.settings import SettingsPage

    cfg, tmp = env
    cfg.profiles["gone"]["backend"] = "stove-czn"
    prefix = tmp / "Games" / "Nadare" / "Gone" / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(tmp / "Games" / "Nadare" / "Gone", prefix)
    _profile_with_backup(cfg, prefix, backup)
    calls: list[tuple] = []
    monkeypatch.setattr(
        prefixmove,
        "delete_backup",
        lambda config, name, path: calls.append((config, name, path)) or path,
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    page = SettingsPage()
    try:
        page.load(cfg, "gone")
        assert page._backup_label.isVisibleTo(page)
        assert f"Old prefix backup: {backup}" in page._backup_label.text()
        assert page._backup_button.isVisibleTo(page)
        page._backup_button.click()
        assert calls == [(cfg, "gone", backup)]
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 5. review findings (scrutiny s5)
# ---------------------------------------------------------------------------


def _backup_trees(cfg):
    return [t for t in removal.removable_trees(cfg, "gone") if t.key.startswith("prefix_backup")]


def test_a_live_prefix_that_is_a_symlink_to_the_backup_is_never_deleted_with_it(env):
    cfg, tmp = env
    wrapper = tmp / "Games" / "Nadare" / "Gone"
    backup = _backup(wrapper, wrapper / "prefix")  # a rolled-back prefix: live = link to backup
    (backup / "drive_c" / "save.dat").write_text("precious")
    live = wrapper / "prefix"
    live.symlink_to(backup, target_is_directory=True)
    _profile_with_backup(cfg, live, backup)

    (tree,) = _backup_trees(cfg)
    assert not tree.safe and "current" in tree.reason
    with pytest.raises(prefixmove.PrefixMoveError):
        prefixmove.delete_backup(cfg, "gone", backup)
    result = removal.remove_game(
        cfg, "gone", delete_trees=[tree.key], applications_dir=tmp / "apps"
    )
    assert result.trees_removed == []
    assert (backup / "drive_c" / "save.dat").read_text() == "precious"


def test_a_backup_holding_the_live_prefix_is_never_deleted(env):
    cfg, tmp = env
    backup = _backup(tmp / "Games", tmp / "Games" / "prefix")
    live = backup / "inner-prefix"
    live.mkdir()
    _profile_with_backup(cfg, live, backup)
    (tree,) = _backup_trees(cfg)
    assert not tree.safe


def test_a_live_prefix_inside_the_backup_through_extra_is_also_protected(env):
    cfg, tmp = env
    backup = _backup(tmp / "Games", tmp / "Games" / "prefix")
    live = backup / "drive_c" / "game-prefix"
    live.mkdir()
    cfg.profiles["gone"].update(prefix_backups=[str(backup)], extra={"wineprefix": str(live)})
    (tree,) = _backup_trees(cfg)
    assert not tree.safe


def test_a_relative_prefix_does_not_make_the_backup_depend_on_the_working_directory(
    env, monkeypatch
):
    cfg, tmp = env
    monkeypatch.chdir(tmp)
    prefix = tmp / "rel" / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(tmp / "rel", prefix)
    cfg.profiles["gone"].update(prefix="rel/prefix", prefix_backups=[str(backup)])
    (here,) = _backup_trees(cfg)
    other = tmp / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    (there,) = _backup_trees(cfg)
    assert here.safe and there.safe, (here.reason, there.reason)


@pytest.mark.parametrize(
    "name",
    ["prefix.broken-", "prefix.broken-notes", "prefix.broken-2026", "prefix.broken-20260102-0304",
     "prefix.broken-20260102-030405-x", "notabackup"],
)
def test_only_a_name_the_rebuild_makes_is_accepted(env, name):
    cfg, tmp = env
    prefix = tmp / "Games" / "prefix"
    prefix.mkdir(parents=True)
    odd = tmp / "Games" / name
    odd.mkdir()
    _profile_with_backup(cfg, prefix, odd)
    (tree,) = _backup_trees(cfg)
    assert not tree.safe and "not named like an old prefix backup" in tree.reason


@pytest.mark.parametrize("suffix", ["20260102-030405", "20260102-030405-2", "20260102-030405-13"])
def test_the_names_the_rebuild_makes_are_accepted(env, suffix):
    cfg, tmp = env
    prefix = tmp / "Games" / "prefix"
    prefix.mkdir(parents=True)
    backup = _backup(tmp / "Games", prefix, suffix)
    _profile_with_backup(cfg, prefix, backup)
    (tree,) = _backup_trees(cfg)
    assert tree.safe, tree.reason


def test_a_ticked_backup_is_still_the_one_deleted_if_an_earlier_one_vanished(env):
    cfg, tmp = env
    wrapper = tmp / "Games" / "Nadare" / "Gone"
    prefix = wrapper / "prefix"
    prefix.mkdir(parents=True)
    first = _backup(wrapper, prefix, "20260101-000000")
    second = _backup(wrapper, prefix, "20260102-000000")
    _profile_with_backup(cfg, prefix, first, second)
    key = next(t.key for t in _backup_trees(cfg) if t.path == second.resolve())
    first_gone = first
    import shutil

    shutil.rmtree(first_gone)  # disappears between the dialog and the deletion
    result = removal.remove_game(cfg, "gone", delete_trees=[key], applications_dir=tmp / "apps")
    assert result.trees_removed == [second.resolve()]
    assert not second.exists()


def test_settings_offers_the_first_backup_that_can_really_be_deleted(env, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui.settings import SettingsPage

    cfg, tmp = env
    cfg.profiles["gone"]["backend"] = "stove-czn"
    wrapper = tmp / "Games" / "Nadare" / "Gone"
    prefix = wrapper / "prefix"
    prefix.mkdir(parents=True)
    stale = wrapper / "prefix.broken-notes"  # exists but is not one a rebuild made
    stale.mkdir()
    valid = _backup(wrapper, prefix, "20260102-030405")
    _profile_with_backup(cfg, prefix, stale, valid)
    cfg.save()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    page = SettingsPage()
    try:
        page.load(cfg, "gone")
        assert f"Old prefix backup: {valid}" in page._backup_label.text()
        page._backup_button.click()
        assert not valid.exists() and stale.is_dir()
        assert not page._backup_button.isVisibleTo(page)
    finally:
        page.deleteLater()
        qapp.processEvents()
