# SPDX-License-Identifier: GPL-3.0-or-later
"""Runner picker labels, runner usage and the Manage runners dialog."""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QPushButton
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import runners
from nadare.core.config import Config
from nadare.gui.runners_dialog import ManageRunnersDialog, human_size, status_text
from nadare.gui.settings import SYSTEM_WINE, SettingsPage, discover_runners


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


def _make(root: Path, name: str, payload: int = 100) -> Path:
    d = root / name
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    (d / "blob").write_bytes(b"x" * payload)
    return d


def _wait(qapp, predicate, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- picker ------------------------------------------------------------------
def test_picker_marks_the_recommended_build_and_the_default(root):
    _make(root, "dwproton-11.0-13-x86_64")
    _make(root, "dwproton-11.0-15-x86_64")
    _make(root, "MyCustomProton")
    entries = dict(discover_runners("stove-czn"))
    labels = list(entries)
    assert labels[0].startswith("Default · recommended") and entries[labels[0]] == ""
    assert "dwproton-11.0-15-x86_64 · recommended" in entries
    assert "dwproton-11.0-13-x86_64 · untested" in entries
    assert "MyCustomProton" in entries
    assert SYSTEM_WINE not in entries  # the Proton games no longer offer system Wine


def test_generic_backends_keep_system_wine_as_their_empty_default(root):
    _make(root, "dwproton-11.0-15-x86_64")
    entries = discover_runners("wine")
    assert entries[0][0].startswith("dwproton")
    assert entries[-1] == (SYSTEM_WINE, "")


def test_a_family_id_in_a_profile_is_not_shown_as_missing(root, tmp_path, qapp):
    _make(root, "dwproton-11.0-15-x86_64")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", runner="dwproton")
    page = SettingsPage()
    try:
        page.load(config, "czn")
        assert "Missing" not in page.runner.currentText()
        assert "recommended" in page.runner.currentText()
    finally:
        page.deleteLater()


# ---- usage ---------------------------------------------------------------------
def test_usage_by_path_family_default_latest_and_system(root):
    pinned = _make(root, "dwproton-11.0-15-x86_64")
    other = _make(root, "dwproton-11.0-13-x86_64")
    profiles = {
        "by_path": {"backend": "wine", "runner": str(other)},
        "by_family": {"backend": "stove-czn", "runner": "dwproton"},
        "by_default": {"backend": "arknights-yostar"},
        "system": {"backend": "stove-czn", "runner": "system"},
        "generic_default": {"backend": "wine"},
    }
    usage = runners.runner_usage(profiles)
    assert sorted(usage[str(pinned)]) == ["by_default", "by_family"]
    assert usage[str(other)] == ["by_path"]


def test_usage_by_folder_name_and_latest(root):
    older = _make(root, "dwproton-11.0-13-x86_64")
    newer = _make(root, "dwproton-11.0-16-x86_64")
    usage = runners.runner_usage(
        {
            "a": {"backend": "wine", "runner": older.name},
            "b": {"backend": "wine", "runner": "latest"},
        }
    )
    assert usage[str(older)] == ["a"]
    assert usage[str(newer)] == ["b"]


def test_runner_size_counts_files_and_ignores_symlinks(tmp_path):
    d = tmp_path / "r"
    (d / "sub").mkdir(parents=True)
    (d / "a").write_bytes(b"1" * 10)
    (d / "sub" / "b").write_bytes(b"2" * 20)
    os.symlink(d / "a", d / "link")
    assert runners.runner_size(d) == 30
    assert runners.runner_size(tmp_path / "missing") == 0


def test_human_size_and_status_text():
    assert human_size(512) == "512 B"
    assert human_size(1536 * 1024 * 1024) == "1.5 GB"
    assert status_text(Path("dwproton-11.0-15-x86_64")) == "recommended (tested)"
    assert status_text(Path("dwproton-11.0-13-x86_64")) == "untested"
    assert status_text(Path("Custom")) == "custom"


# ---- dialog --------------------------------------------------------------------
def _delete_button(dialog, name):
    row = next(i for i in range(dialog.table.rowCount()) if dialog.table.item(i, 0).text() == name)
    return dialog.table.cellWidget(row, 4)


def test_dialog_lists_runners_with_sizes_and_disables_the_ones_in_use(root, qapp):
    _make(root, "dwproton-11.0-13-x86_64", payload=4096)
    _make(root, "dwproton-11.0-15-x86_64", payload=2048)
    profiles = {"czn": {"backend": "stove-czn"}}
    dialog = ManageRunnersDialog(profiles)
    try:
        assert dialog.table.rowCount() == 2
        assert _wait(qapp, lambda: "…" not in {dialog.table.item(i, 2).text() for i in range(2)})
        assert not _delete_button(dialog, "dwproton-11.0-15-x86_64").isEnabled()
        assert "czn" in _delete_button(dialog, "dwproton-11.0-15-x86_64").toolTip()
        assert _delete_button(dialog, "dwproton-11.0-13-x86_64").isEnabled()
        assert dialog.total_label.text().startswith("Total:")
    finally:
        dialog.deleteLater()


def test_dialog_deletes_an_unused_runner_in_the_background(root, qapp):
    keep = _make(root, "dwproton-11.0-15-x86_64")
    gone = _make(root, "dwproton-11.0-13-x86_64")
    dialog = ManageRunnersDialog({"czn": {"backend": "stove-czn"}})
    try:
        dialog.start_delete(gone)
        assert _wait(qapp, lambda: not gone.exists())
        assert _wait(qapp, lambda: dialog.table.rowCount() == 1)
        assert keep.exists()
        assert dialog.table.item(0, 0).text() == keep.name
    finally:
        dialog.deleteLater()


def test_dialog_refuses_to_delete_a_runner_a_profile_uses(root, qapp, monkeypatch):
    used = _make(root, "dwproton-11.0-15-x86_64")
    shown: list = []
    monkeypatch.setattr(
        "nadare.gui.runners_dialog.QMessageBox.information", lambda *a, **k: shown.append(a[2])
    )
    dialog = ManageRunnersDialog({"czn": {"backend": "stove-czn"}})
    try:
        dialog.start_delete(used)
        qapp.processEvents()
        assert used.exists()
        assert shown and "czn" in shown[0]
    finally:
        dialog.deleteLater()


def test_dialog_with_nothing_installed_says_so(root, qapp):
    dialog = ManageRunnersDialog({})
    try:
        assert dialog.table.rowCount() == 0
        assert not dialog.empty_label.isHidden() or dialog.empty_label.isVisibleTo(dialog)
    finally:
        dialog.deleteLater()


def test_settings_has_a_manage_runners_button(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "czn")
        assert isinstance(page.manage_runners, QPushButton)
        assert "Manage runners" in page.manage_runners.text()
    finally:
        page.deleteLater()


# ---- cleanup of unused old runners ---------------------------------------------------
def test_unused_old_runners_keeps_the_recommended_and_anything_in_use(root):
    keep = _make(root, "dwproton-11.0-15-x86_64")  # recommended
    used = _make(root, "dwproton-11.0-16-x86_64")
    old = _make(root, "dwproton-11.0-13-x86_64")
    older = _make(root, "dwproton-11.0-12-x86_64")
    profiles = {"ark": {"backend": "wine", "runner": str(used)}}
    stale = runners.unused_old_runners(profiles)
    assert sorted(p.name for p in stale) == [older.name, old.name]
    assert keep not in stale and used not in stale


def test_dialog_button_deletes_all_unused_old_runners(root, qapp, monkeypatch):
    keep = _make(root, "dwproton-11.0-15-x86_64")
    used = _make(root, "dwproton-11.0-16-x86_64")
    old = _make(root, "dwproton-11.0-13-x86_64")
    older = _make(root, "dwproton-11.0-12-x86_64")
    profiles = {"ark": {"backend": "wine", "runner": str(used)}}
    dialog = ManageRunnersDialog(profiles)
    try:
        # Usage is checked in the background now; wait for the safe list.
        assert _wait(qapp, lambda: dialog.cleanup_button.isEnabled())
        assert "(2)" in dialog.cleanup_button.text()
        monkeypatch.setattr(
            "nadare.gui.runners_dialog.QMessageBox.question",
            lambda *a, **k: (
                __import__(
                    "PyQt6.QtWidgets", fromlist=["QMessageBox"]
                ).QMessageBox.StandardButton.Yes
            ),
        )
        dialog._confirm_delete_unused()
        assert _wait(qapp, lambda: not old.exists() and not older.exists())
        assert keep.exists() and used.exists()
        assert _wait(qapp, lambda: not dialog.cleanup_button.isEnabled())
    finally:
        dialog.deleteLater()


def test_dialog_button_is_disabled_when_nothing_is_unused(root, qapp):
    _make(root, "dwproton-11.0-15-x86_64")
    dialog = ManageRunnersDialog({"czn": {"backend": "stove-czn"}})
    try:
        assert not dialog.cleanup_button.isEnabled()
    finally:
        dialog.deleteLater()


def _window(tmp_path):
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="CZN")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    return window


def test_a_new_download_triggers_one_cleanup_offer_and_deleting_works(
    root, tmp_path, qapp, monkeypatch
):
    from PyQt6.QtWidgets import QMessageBox

    old = _make(root, "dwproton-11.0-13-x86_64")
    window = _window(tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes,
    )
    try:
        window._start("czn", "install")  # records what is installed
        _make(root, "dwproton-11.0-15-x86_64")  # the operation downloaded the pin
        window._on_succeeded("czn", "install", 0)
        assert _wait(qapp, lambda: bool(asked))
        assert "dwproton-11.0-13" in asked[0] and "recommended build is kept" in asked[0]
        # The meaningful completion condition is the deletion itself, not an
        # intermediate status label (other background work may overwrite it).
        assert _wait(qapp, lambda: not old.exists())
        assert (root / "dwproton-11.0-15-x86_64").exists()
    finally:
        window.close()


def test_no_offer_when_no_runner_was_downloaded_or_nothing_is_unused(
    root, tmp_path, qapp, monkeypatch
):
    asked: list = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.question", lambda *a, **k: asked.append(1)
    )
    _make(root, "dwproton-11.0-13-x86_64")
    _make(root, "dwproton-11.0-15-x86_64")
    window = _window(tmp_path)
    try:
        window._start("czn", "install")
        window._on_succeeded("czn", "install", 0)  # nothing new appeared
        _wait(qapp, lambda: False, 0.5)
        assert asked == []

        window._start("czn", "update")
        _make(root, "dwproton-11.0-16-x86_64")  # new, but the default profile uses the pin
        window._on_succeeded("czn", "update", 0)
        _wait(qapp, lambda: False, 0.5)
        # 11.0-13 and 11.0-16 are unused old builds, so this one does ask once.
        assert len(asked) == 1
    finally:
        window.close()


def test_an_existing_prefix_with_system_wine_still_defaults_to_the_recommended_proton(
    root, tmp_path, qapp, monkeypatch
):
    _make(root, "dwproton-11.0-15-x86_64")
    config = Config.load(tmp_path / "config.json")
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    config.add_profile("ark", backend="arknights-yostar", prefix=str(prefix))
    from nadare.backends import arknights as ak

    monkeypatch.setattr(ak.shutil, "which", lambda name: "/usr/bin/wine")
    page = SettingsPage()
    try:
        page.load(config, "ark")
        assert page.runner.itemText(0).startswith("Default · recommended")
        assert all("system Wine" not in page.runner.itemText(i) for i in range(page.runner.count()))
    finally:
        page.deleteLater()


# ---- deletion safety (audit) ---------------------------------------------------------
def test_removing_a_symlink_removes_the_link_not_its_target(root):
    real = _make(root, "dwproton-11.0-15-x86_64")
    link = root / "old"
    link.symlink_to(real)
    assert runners.remove(link) is True
    assert real.exists() and (real / "proton").is_file(), "the target must survive"
    assert not link.is_symlink() and not link.exists()


def test_remove_still_refuses_anything_outside_the_runners_folder(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "proton").write_text("x")
    assert runners.remove(outside) is False
    link = root / "escape"
    link.symlink_to(outside)
    assert runners.remove(link) is True  # only the link goes
    assert outside.exists()


def test_every_alias_of_a_used_runner_counts_as_used(root):
    real = _make(root, "dwproton-11.0-15-x86_64")
    alias = root / "old"
    alias.symlink_to(real)
    usage = runners.runner_usage({"czn": {"backend": "stove-czn"}})
    assert usage[str(real)] == ["czn"] and usage[str(alias)] == ["czn"]
    assert runners.unused_old_runners({"czn": {"backend": "stove-czn"}}) == []


def test_destructive_questions_default_to_no(root, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    _make(root, "dwproton-11.0-13-x86_64")
    seen: list = []

    def ask(*args, **kwargs):
        seen.append(args[-1] if len(args) >= 5 else kwargs.get("defaultButton"))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("nadare.gui.runners_dialog.QMessageBox.question", ask)
    dialog = ManageRunnersDialog({})
    try:
        assert _wait(qapp, lambda: dialog.cleanup_button.isEnabled())
        dialog._confirm_delete(root / "dwproton-11.0-13-x86_64")
        dialog._confirm_delete_unused()
        assert seen == [QMessageBox.StandardButton.No, QMessageBox.StandardButton.No]
        assert (root / "dwproton-11.0-13-x86_64").exists(), "No must not delete"
    finally:
        dialog.deleteLater()


def test_a_zero_byte_runner_shows_a_size_not_an_ellipsis(root, qapp):
    d = root / "dwproton-11.0-13-x86_64"
    d.mkdir()
    (d / "proton").write_text("")  # empty file: zero bytes
    dialog = ManageRunnersDialog({})
    try:
        assert _wait(qapp, lambda: dialog.table.item(0, 2).text() == "0 B")
    finally:
        dialog.deleteLater()


def test_bulk_delete_disables_the_row_buttons(root, qapp, monkeypatch):
    _make(root, "dwproton-11.0-13-x86_64")
    _make(root, "dwproton-11.0-12-x86_64")
    dialog = ManageRunnersDialog({})
    try:
        assert _wait(qapp, lambda: dialog.cleanup_button.isEnabled())
        real_delete_all = dialog._delete_all
        gate = __import__("threading").Event()
        monkeypatch.setattr(
            dialog, "_delete_all", lambda todo: (gate.wait(3), real_delete_all(todo))
        )
        dialog.start_delete_many(list(dialog._unused))
        assert all(not b.isEnabled() for b in dialog._buttons.values())
        gate.set()
        assert _wait(qapp, lambda: dialog.table.rowCount() == 0)
    finally:
        dialog.deleteLater()


def test_the_cleanup_worker_survives_failures_and_counts_only_real_deletions(
    root, tmp_path, qapp, monkeypatch
):
    good = _make(root, "dwproton-11.0-13-x86_64")
    bad = _make(root, "dwproton-11.0-12-x86_64")
    window = _window(tmp_path)
    real_remove = runners.remove

    def flaky(path):
        if Path(path) == bad:
            raise OSError("permission denied")
        return real_remove(path)

    monkeypatch.setattr(runners, "remove", flaky)
    try:
        window._delete_stale([good, bad])
        assert _wait(qapp, lambda: "Deleted 1 unused runner" in window.side_status.text())
        assert "1 could not be deleted" in window.side_status.text()
        assert not good.exists() and bad.exists()
    finally:
        window.close()


def test_manage_runners_counts_an_unsaved_selection_as_in_use(root, tmp_path, qapp, monkeypatch):
    old = _make(root, "dwproton-11.0-13-x86_64")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")  # saved runner: default
    page = SettingsPage()
    captured: dict = {}

    class FakeDialog:
        def __init__(self, profiles, parent=None):
            captured["profiles"] = profiles

        def exec(self):
            return 0

    monkeypatch.setattr("nadare.gui.runners_dialog.ManageRunnersDialog", FakeDialog)
    try:
        page.load(config, "czn")
        page.runner.setEditText(str(old))  # the user typed this, not saved yet
        page._open_runners_dialog()
        assert captured["profiles"]["czn"]["runner"] == str(old)
        assert "runner" not in config.profile("czn") or not config.profile("czn")["runner"]
        usage = runners.runner_usage(captured["profiles"])
        assert usage[str(old)] == ["czn"]
    finally:
        page.deleteLater()


def test_manage_runners_keeps_the_default_runner_empty(root, tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()

    class FakeDialog:
        def __init__(self, profiles, parent=None):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr("nadare.gui.runners_dialog.ManageRunnersDialog", FakeDialog)
    try:
        page.load(config, "czn")
        page._open_runners_dialog()
        assert page._runner_value() == ""
    finally:
        page.deleteLater()
