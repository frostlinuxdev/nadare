# SPDX-License-Identifier: GPL-3.0-or-later
"""The five sweep findings still open on 2026-11-08 (#331, #338, #339, #351), each re-checked."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from nadare.core import diskspace, hostlibs

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False

needs_qt = pytest.mark.skipif(not HAVE_QT, reason="PyQt6 is not importable")


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


# ---- #351 F-16: no "image(s)" -------------------------------------------------------------------
@needs_qt
@pytest.mark.parametrize(("count", "word"), [(1, "1 image "), (3, "3 images ")])
def test_the_art_note_counts_in_words(qapp, tmp_path, count, word):
    from nadare.gui.settings import SettingsPage

    page = SettingsPage()
    try:
        page._profile = "g"
        page._config = object()  # a loaded config; the handler only checks it is set
        files = {f"k{i}": str(tmp_path / f"c{i}.png") for i in range(count)}
        page._on_art_fetched("g", files)
        assert word in page.note.text() and "(s)" not in page.note.text()
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #331 F-10: a working custom runner is not "Missing" ------------------------------------
@needs_qt
def test_a_custom_runner_folder_is_not_called_missing(qapp, tmp_path):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    runner = tmp_path / "opt" / "GE-Proton9-1"
    runner.mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    config = Config.load(tmp_path / "config.json")
    config.profiles["g"] = {"backend": "wine", "install_dir": str(tmp_path / "g"),
                            "runner": str(runner)}
    page = SettingsPage()
    try:
        page.load(config, "g")
        qapp.processEvents()
        assert not page.runner.currentText().startswith("Missing")
        assert "GE-Proton9-1" in page.runner.currentText()
        config.profiles["g"]["runner"] = str(tmp_path / "gone")
        page.load(config, "g")
        qapp.processEvents()
        assert page.runner.itemText(0).startswith("Missing")  # a real missing one still says so
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #331 F-11: a Steam app id must be a number -----------------------------------------------
@needs_qt
def test_a_steam_app_id_that_is_not_a_number_is_refused_at_save(qapp, tmp_path, monkeypatch):
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    config = Config.load(tmp_path / "config.json")
    config.profiles["s"] = {"backend": "steam", "steam_app_id": "4813850"}
    config.save()
    page = SettingsPage()
    try:
        page.load(config, "s")
        qapp.processEvents()
        page.option_widget("steam_app_id").setText("not-a-number")
        page.save()
        qapp.processEvents()
        assert Config.load(tmp_path / "config.json").profiles["s"]["steam_app_id"] == "4813850"
        assert any("number" in w.lower() for w in warned) or "number" in page.note.text().lower()
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #338 F-08: the missing-libraries command can be copied ------------------------------------
_HINT = (
    "Missing 32-bit libraries: libfreetype.so.6.\nwhy\nInstall them with:\n"
    "    sudo apt install libfreetype6:i386\nOr install umu-launcher (umu-run), which brings "
    "its own runtime."
)


def test_the_install_command_is_found_in_the_hint():
    assert hostlibs.install_command_in(_HINT) == "sudo apt install libfreetype6:i386"
    assert hostlibs.install_command_in("Missing 32-bit libraries: x.\nInstall umu.") == ""
    assert hostlibs.install_command_in("some other error") == ""


@needs_qt
def test_the_failure_box_offers_to_copy_the_install_command(qapp, monkeypatch):
    from nadare.gui.game_view import GameView

    def click_copy(self):
        next(b for b in self.buttons() if b.text() == "Copy install command").click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", click_copy)
    page = GameView()
    try:
        choice = page._real_failure_choice("Launch failed", _HINT, True)
        assert choice == "close"
        assert QApplication.clipboard().text() == "sudo apt install libfreetype6:i386"
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #339 F-10: the CLI install checks the space first ------------------------------------------
def test_the_cli_install_checks_the_game_size_before_installing(tmp_path, monkeypatch):
    from nadare import app

    called: list[str] = []

    class Backend:
        install_size_hint = 50 * 2**30

        def install_dir(self):
            return tmp_path / "game"

        def install(self):
            called.append("install")

    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1 * 2**30)
    with pytest.raises(Exception) as caught:
        app._check_install_space(Backend())
    assert "space" in str(caught.value).lower() or "room" in str(caught.value).lower()
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 500 * 2**30)
    app._check_install_space(Backend())  # enough room: no error


def test_a_backend_without_a_size_is_not_checked(tmp_path):
    from nadare import app

    class Backend:
        def install_dir(self):
            return tmp_path

    app._check_install_space(Backend())
    assert Path(tmp_path).exists()
