# SPDX-License-Identifier: GPL-3.0-or-later
"""Store Install: one combined confirm, then the profile exists and the install runs."""

from __future__ import annotations

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.quick_install import QuickInstallDialog

KIND = "stellasora-yostar"  # unofficial notice + a multi-GB download


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _record(started):
    def start(profile, op, *args, **kwargs):
        if op == "install":
            started.append((profile, op))
        return True

    return start


def _window(tmp_path, monkeypatch, *, acked=()):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    cfg = Config.load(tmp_path / "config.json")
    if acked:
        cfg.data["unofficial_notice_ack"] = list(acked)
    window = MainWindow(cfg)
    started = []
    monkeypatch.setattr(window, "_start", _record(started))
    shown = []

    def fake_exec(self):
        shown.append(self)
        return QDialog.DialogCode.Accepted if answer[0] else QDialog.DialogCode.Rejected

    answer = [True]
    monkeypatch.setattr(QuickInstallDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("unexpected message box"))
    return window, cfg, started, shown, answer


def test_store_install_makes_one_profile_and_starts_one_install(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    try:
        window._store_install(KIND)
        assert len(shown) == 1
        assert len(cfg.profiles) == 1
        name = next(iter(cfg.profiles))
        assert cfg.profiles[name]["backend"] == KIND
        assert started == [(name, "install")]
        assert window._current_profile == name
        assert Config.load(tmp_path / "config.json").profiles.get(name)
    finally:
        window.close()


def test_cancelling_creates_nothing(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, answer = _window(tmp_path, monkeypatch)
    answer[0] = False
    try:
        window._store_install(KIND)
        assert len(shown) == 1
        assert cfg.profiles == {} and started == []
        assert "unofficial_notice_ack" not in cfg.data
    finally:
        window.close()


def test_the_notice_is_shown_once_and_remembered(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    try:
        window._store_install(KIND)
        assert shown[0].notice_text  # first time: the notice is part of the dialog
        assert cfg.data["unofficial_notice_ack"] == [KIND]
        assert Config.load(tmp_path / "config.json").data["unofficial_notice_ack"] == [KIND]
        window._store_install(KIND)
        assert shown[1].notice_text == ""  # already acknowledged
        assert len(cfg.profiles) == 2 and len(started) == 2
    finally:
        window.close()


def test_the_dialog_shows_title_size_and_free_space(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=[KIND])
    try:
        window._store_install(KIND)
        dialog = shown[0]
        assert "GiB" in dialog.summary_text
        assert dialog.folder_text
    finally:
        window.close()


def test_changing_the_location_is_used_for_the_profile(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=[KIND])
    library = tmp_path / "games"
    library.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(library))
    original = QuickInstallDialog.exec

    def pick_then_accept(self):
        self.change_location()
        return original(self)

    monkeypatch.setattr(QuickInstallDialog, "exec", pick_then_accept)
    try:
        window._store_install(KIND)
        profile = next(iter(cfg.profiles.values()))
        assert str(library) in profile["install_dir"]
        # what the dialog showed is exactly where the game went
        assert shown[0].folder_text == profile["install_dir"]
    finally:
        window.close()


def test_czn_shows_and_uses_the_prefix_as_its_folder(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=["stove-czn"])
    library = tmp_path / "games"
    library.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(library))
    original = QuickInstallDialog.exec

    def pick_then_accept(self):
        self.change_location()
        return original(self)

    monkeypatch.setattr(QuickInstallDialog, "exec", pick_then_accept)
    try:
        window._store_install("stove-czn")
        profile = next(iter(cfg.profiles.values()))
        assert shown[0].folder_text == profile["prefix"]
        assert str(library) in profile["prefix"]
        assert not profile.get("install_dir")
    finally:
        window.close()


def test_free_space_follows_the_chosen_folder(tmp_path, qapp, monkeypatch):
    from nadare.core import diskspace

    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=[KIND])
    library = tmp_path / "games"
    library.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(library))
    monkeypatch.setattr(
        diskspace, "free_bytes", lambda path: (500 << 30) if "games" in str(path) else (3 << 30)
    )
    original = QuickInstallDialog.exec
    seen = []

    def pick_then_accept(self):
        seen.append(self.summary_text)
        self.change_location()
        seen.append(self.summary_text)
        return original(self)

    monkeypatch.setattr(QuickInstallDialog, "exec", pick_then_accept)
    try:
        window._store_install(KIND)
        assert "(3 GiB free)" in seen[0] and "500 GiB free" in seen[1] and "3 GiB" not in seen[1]
    finally:
        window.close()


def test_an_unreadable_disk_does_not_crash_the_dialog(tmp_path, qapp, monkeypatch):
    from nadare.core import diskspace
    from nadare.core.backends import BackendError

    def broken(path):
        raise BackendError("stale mount")

    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=[KIND])
    monkeypatch.setattr(diskspace, "free_bytes", broken)
    try:
        window._store_install(KIND)
        assert "unknown" in shown[0].summary_text and len(cfg.profiles) == 1
    finally:
        window.close()


def test_I_already_have_it_opens_the_full_dialog_and_creates_nothing(
    tmp_path, qapp, monkeypatch
):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(window, "_add_game_dialog", lambda **k: opened.append(k))

    def press_import(self):
        self.import_button.click()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QuickInstallDialog, "exec", press_import)
    try:
        window._store_install(KIND)
        assert opened == [{"kind": KIND}]
        assert cfg.profiles == {} and started == []
    finally:
        window.close()


def test_a_taken_name_gets_a_free_one(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=[KIND])
    cfg.add_profile("stellasora", backend="wine", title="Mine")
    cfg.save()
    try:
        window._store_install(KIND)
        assert len(cfg.profiles) == 2
        assert cfg.profiles["stellasora"]["backend"] == "wine"  # untouched
        assert started and started[0][0] != "stellasora"
    finally:
        window.close()


def test_the_sidebar_add_game_path_is_unchanged(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(window, "_add_game_dialog", lambda **k: opened.append(k))
    try:
        window.store.own_game_requested.emit()
        assert opened == [{"advanced": True}]
        assert shown == [] and cfg.profiles == {}
    finally:
        window.close()


def test_the_store_install_button_goes_to_the_one_step_path(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(window, "_store_install", lambda kind: seen.append(kind))
    try:
        window.store.install_requested.emit(KIND)
        assert seen == [KIND]
    finally:
        window.close()
