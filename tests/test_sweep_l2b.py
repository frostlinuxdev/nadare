# SPDX-License-Identifier: GPL-3.0-or-later
"""Sweep L2b: Settings, Add game and dialog wording/behaviour fixes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path, kind: str, name: str = "sample", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend=kind, **profile)
    return config


# ---- #331 F-06: hide the runner row where a runner is never used -------------
def test_runner_picker_is_hidden_for_kinds_that_do_not_use_a_runner(tmp_path, qapp):
    page = SettingsPage()
    try:
        for kind in ("steam", "fgo-lepton", "script"):
            page.load(_config(tmp_path / f"{kind}.json", kind), "sample")
            assert not page.runner.isVisibleTo(page), f"{kind} must not offer a runner"

        for kind in ("wine", "stove-czn", "arknights-yostar", "stellasora-yostar"):
            page.load(_config(tmp_path / f"{kind}.json", kind), "sample")
            assert page.runner.isVisibleTo(page), f"{kind} still uses a runner"
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #331 F-08: malformed Environment lines must not vanish silently --------
def test_malformed_environment_line_is_reported_and_valid_lines_are_saved(
    tmp_path, qapp, monkeypatch
):
    shown: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.settings.QMessageBox.open", lambda self, *a: shown.append(self.text())
    )
    config = _config(tmp_path, "arknights-yostar")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.option_widget("extra_env").setPlainText(
            "GOOD=1\nTHIS LINE HAS NO EQUALS\n# comment\nEMPTY="
        )
        page.save()
        saved = config.profile("sample")["extra_env"]
        assert saved == {"GOOD": "1", "EMPTY": ""}
        assert shown, "a discarded Environment line must be reported"
        assert "Environment" in shown[0]
        assert "no equals" in shown[0].lower() or "=" in shown[0]
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #331 F-13: the remember hint must match when the login is removed ------
def test_remember_hint_points_at_save_instead_of_immediate_deletion(tmp_path, qapp):
    page = SettingsPage()
    try:
        page.load(_config(tmp_path, "stove-czn"), "sample")
        hint = page.remember_hint.text()
        assert "Save" in hint
        assert "immediately" not in hint.lower()
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #331 F-14: leaving with unsaved edits asks first -----------------------
def test_back_asks_before_discarding_unsaved_changes(tmp_path, qapp, monkeypatch):
    answers: list[object] = [None]
    asked: list[str] = []

    def question(_parent, _title, text, *args, **kwargs):
        asked.append(text)
        return answers[0]

    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr("nadare.gui.settings.QMessageBox.question", question)
    left: list[bool] = []
    page = SettingsPage()
    page.cancelled.connect(lambda: left.append(True))
    try:
        page.load(_config(tmp_path, "wine"), "sample")
        page.back.click()
        assert asked == [] and len(left) == 1, "an unchanged form leaves immediately"

        page.title.setText("changed")
        answers[0] = QMessageBox.StandardButton.No
        page.back.click()
        assert asked and asked[-1] == "Discard your changes?"
        assert len(left) == 1, "No keeps the page open"

        answers[0] = QMessageBox.StandardButton.Yes
        page.back.click()
        assert len(left) == 2, "Yes discards the edits and leaves"
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #351 F-09: one way to leave Settings ----------------------------------
def test_settings_has_only_one_back_or_cancel_action(tmp_path, qapp):
    from PyQt6.QtWidgets import QPushButton

    page = SettingsPage()
    try:
        page.load(_config(tmp_path, "wine"), "sample")
        labels = [button.text() for button in page.findChildren(QPushButton)]
        assert "Cancel" not in labels
        assert sum(1 for text in labels if "Back" in text) == 1
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #351 F-06: the same folder is called "Game folder" everywhere ----------
def test_add_game_calls_the_folder_game_folder(tmp_path, qapp):
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), data_root=tmp_path / "data")
    try:
        assert dialog.folder_label.text() == "Game folder"
        dialog.set_mode(True)
        assert "game folder" in dialog.folder_label.text().lower()
        assert "install folder" not in dialog.folder_label.text().lower()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- #351/#335 F-07: prefix placeholder and import button ------------------
def test_prefix_placeholder_fits_the_mode(tmp_path, qapp):
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), data_root=tmp_path / "data")
    try:
        dialog.set_backend("wine")
        assert dialog.prefix.placeholderText() == "Created automatically"
        dialog.set_mode(True)
        assert dialog.prefix.placeholderText() == "Folder of the existing install"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_import_button_is_disabled_until_detection_finds_something(tmp_path, qapp):
    empty = tmp_path / "empty"
    empty.mkdir()
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_mode(True)
        dialog.set_install_dir(empty)
        dialog.detect()
        assert dialog.detect_label.text() == "Nothing detected yet."
        assert not dialog.create_button.isEnabled()

        found = tmp_path / "found"
        found.mkdir()
        (found / "Arknights.exe").write_text("MZ")
        dialog.set_install_dir(found)
        dialog.detect()
        assert dialog.create_button.isEnabled()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    prefix = tmp_path / "czn-prefix"
    prefix.mkdir()
    czn = AddGameDialog(Config.load(tmp_path / "czn.json"), data_root=tmp_path / "data")
    try:
        czn.set_backend("stove-czn")
        czn.set_mode(True)
        czn.set_install_dir(prefix)
        czn.detect()
        assert czn.detect_label.text() == "Nothing detected yet."
        assert not czn.create_button.isEnabled(), "an empty folder is not a CZN prefix"

        (prefix / "drive_c").mkdir()
        czn.detect()
        assert czn.detect_label.text() != "Nothing detected yet."
        assert czn.create_button.isEnabled(), "CZN needs only the prefix, which was found"
    finally:
        czn.deleteLater()
        qapp.processEvents()


# ---- #335 F-12: say that Install new does not download yet, and the size ----
def test_install_new_explains_the_next_step_and_the_size(tmp_path, qapp):
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        text = dialog.size_hint.text()
        assert "Press INSTALL" in text
        assert "about" in text
        dialog.set_backend("steam")
        assert "Press INSTALL" in dialog.size_hint.text()
        assert "about" not in dialog.size_hint.text()
        dialog.set_mode(True)
        assert dialog.size_hint.isHidden()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- #332 F-08: the config-recovery notice says what to do next -------------
def test_config_recovery_notice_adds_a_plain_next_step(tmp_path, qapp):
    from nadare.gui import _config_recovery_text

    config = Config.load(tmp_path / "config.json")
    backup = tmp_path / "config.json.bad-20260101"
    config.notice = (
        f"{config.path} was unreadable (not valid JSON); it was moved to {backup} "
        "and a fresh config is in use"
    )
    text = _config_recovery_text(config)
    assert "Your settings were reset." in text
    assert f"A copy of the old file was kept at {backup}." in text
    assert "You can add your games again." in text
