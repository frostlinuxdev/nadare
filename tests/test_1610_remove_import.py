# SPDX-License-Identifier: GPL-3.0-or-later
"""Catalog installs cannot adopt arbitrary game folders or prefixes."""

import importlib
import json

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from nadare.core.config import Config
from nadare.core.directkinds import HELD_BACK_KINDS
from nadare.gui.add_game import CATALOG, AddGameDialog
from nadare.gui.main_window import MainWindow
from nadare.gui.quick_install import QuickInstallDialog


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_quick_install_has_no_import_controls(qapp):
    dialog = QuickInstallDialog("Game", notice="", describe=lambda _: ("folder", "", ""))
    try:
        for attribute in ("import_button", "import_requested", "_want_import"):
            assert not hasattr(dialog, attribute)
        assert not any("already have" in b.text() for b in dialog.findChildren(QPushButton))
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize(
    "kind",
    [g.kind for g in CATALOG if g.group == "Games" and g.kind not in HELD_BACK_KINDS],
)
def test_catalog_dialog_has_no_import_mode_or_direct_prefix_picker(tmp_path, qapp, kind):
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), kind=kind)
    try:
        for attribute in ("import_existing", "install_new", "set_mode", "created_by_import"):
            assert not hasattr(dialog, attribute)
        if kind.startswith("direct-"):
            dialog.show()
            dialog.location_toggle.click()
            assert not dialog.prefix_toggle.isVisible()
            assert not dialog.prefix.isVisible()
            dialog._toggle_prefix()
            assert not dialog.prefix.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_detect_import_is_no_longer_exported():
    with pytest.raises(ImportError):
        exec("from nadare.gui.add_game import detect_import", {})
    assert not hasattr(importlib.import_module("nadare.gui.add_game"), "detect_import")


def test_fgo_keeps_an_explicit_existing_lepton_prefix(tmp_path, qapp):
    prefix = tmp_path / "lepton-nv"
    (prefix / "lepton-x86_64").mkdir(parents=True)
    (prefix / "lepton-x86_64" / "lepton-fgo").write_text("#!/bin/sh\n")
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"), kind="fgo-lepton")
    try:
        dialog.show()
        dialog.location_toggle.click()
        assert dialog.prefix.isVisible()
        assert not dialog.prefix_only_summary.isVisible()
        dialog.set_prefix(prefix)
        name = dialog.create_profile()
        assert name
        assert dialog.config.profiles[name]["prefix"] == str(prefix)
        assert not dialog.config.profiles[name].get("owned_trees")
    finally:
        dialog.deleteLater()


def test_add_game_has_no_post_import_move_offer():
    assert not hasattr(MainWindow, "_maybe_offer_library_move")


@pytest.mark.parametrize("accepted", [False, True])
def test_store_never_opens_import_dialog(tmp_path, qapp, monkeypatch, accepted):
    monkeypatch.delenv("NADARE_ASSUME_NOTICES", raising=False)
    window = MainWindow(Config.load(tmp_path / "config.json"))
    started = []

    def confirm(dialog):
        # Even stale callers setting the old flag cannot select an import path.
        dialog.import_requested = True
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(QuickInstallDialog, "exec", confirm)
    monkeypatch.setattr(window, "_add_game_dialog", lambda **kw: pytest.fail(str(kw)))
    monkeypatch.setattr(window, "_start", lambda *a, **kw: started.append(a))
    try:
        window._store_install("direct-snowbreak")
        assert bool(window.config.profiles) == accepted
        assert any(args[1] == "install" for args in started) == accepted
    finally:
        window.close()


def test_old_import_opens_and_remove_keeps_external_folders(tmp_path, qapp, monkeypatch):
    install, prefix = tmp_path / "external-game", tmp_path / "external-prefix"
    for folder in (install, prefix):
        folder.mkdir()
        (folder / "keep.txt").write_text("player data")
    profile = {
        "backend": "direct-snowbreak",
        "title": "Snowbreak",
        "install_dir": str(install),
        "prefix": str(prefix),
        "made_by_nadare": [],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"profiles": {"snowbreak": profile}}))
    config = Config.load(config_path)
    window = MainWindow(config)

    class Pool:
        def start(self, task):
            task.run()

    from nadare.gui import main_window as mw

    monkeypatch.setattr(mw.QThreadPool, "globalInstance", lambda: Pool())
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    try:
        assert "snowbreak" in window.library._cards
        window.open_game("snowbreak")
        assert window._current_profile == "snowbreak"
        assert config.profiles["snowbreak"]["prefix"] == str(prefix)
        window._on_remove_requested("snowbreak")
        assert "snowbreak" not in config.profiles
        assert "snowbreak" not in Config.load(config_path).profiles
        for folder in (install, prefix):
            assert (folder / "keep.txt").read_text() == "player data"
    finally:
        window.close()
