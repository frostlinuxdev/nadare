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


def test_the_store_sheet_names_the_download_and_the_peak_free_space(tmp_path, qapp, monkeypatch):
    """BPSR downloads 40 GiB and keeps the 40 GiB zip while it unpacks to 45 GiB: say both."""
    from nadare.core import backends, diskspace
    from nadare.core.diskspace import human_size

    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    try:
        window._store_install("direct-bpsr")
        assert len(shown) == 1
        summary = shown[0].summary_text
        download = human_size(backends.download_size_hint("direct-bpsr"))
        peak = human_size(diskspace.free_needed(backends.install_space_hint("direct-bpsr")))
        assert f"downloads about {download}" in summary
        assert f"needs about {peak} free space" in summary
        assert started and started[0][1] == "install"
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
        assert cfg.data["unofficial_notice_ack"] is True  # accepted once for the whole app
        assert Config.load(tmp_path / "config.json").data["unofficial_notice_ack"] is True
        # Reinstalling after removing the game keeps the acknowledgement.
        (name,) = cfg.profiles
        cfg.remove_profile(name)
        cfg.save()
        window._store_install(KIND)
        assert shown[1].notice_text == ""  # already acknowledged
        assert len(cfg.profiles) == 1 and len(started) == 2
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


def test_the_store_install_button_goes_to_the_one_step_path(tmp_path, qapp, monkeypatch):
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(window, "_store_install", lambda kind: seen.append(kind))
    try:
        window.store.install_requested.emit(KIND)
        assert seen == [KIND]
    finally:
        window.close()


def test_a_game_whose_download_fits_its_space_says_so(tmp_path, qapp, monkeypatch):
    """No separate peak: the sheet says "needs about as much free space"."""
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=["direct-pgr"])
    try:
        window._store_install("direct-pgr")
        assert len(shown) == 1
        summary = shown[0].summary_text
        assert "downloads about" in summary
        assert "needs about as much free space" in summary
        assert started and started[0][1] == "install"
    finally:
        window.close()


def test_picking_a_smaller_quality_updates_the_space_figure(tmp_path, qapp, monkeypatch):
    """WuWa's picked quality is both the download and what must fit (space_need)."""
    window, cfg, started, shown, _ = _window(tmp_path, monkeypatch, acked=["direct-wuwa"])
    seen = []

    def pick_sd_then_accept(dialog):
        seen.append(dialog.summary_text)
        dialog.choice_combo.setCurrentIndex(0)  # SD, 58 GiB
        seen.append(dialog.summary_text)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QuickInstallDialog, "exec", pick_sd_then_accept)
    try:
        window._store_install("direct-wuwa")
        assert "80 GiB" in seen[0]  # the default HD quality
        assert "58 GiB" in seen[1]
        assert "needs about 58 GiB free space" not in seen[1]
        assert "needs about as much free space" in seen[1]
        assert started and started[0][1] == "install"
    finally:
        window.close()
