# SPDX-License-Identifier: GPL-3.0-or-later
"""The Add game wizard: new profile creation."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.core.diskspace import human_size
from nadare.gui.add_game import AddGameDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_install_new_uses_defaults_under_the_data_root(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        folder = dialog.folder.text()
        assert folder.startswith(str(tmp_path / "data"))
        assert dialog.create_profile() == "arknights"
        profile = config.profile("arknights")
        assert profile["backend"] == "arknights-yostar"
        assert profile["install_dir"] == folder
        assert profile["prefix"] == str(tmp_path / "data" / "arknights" / "prefix")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_size_hint_is_the_download_not_the_unpacking_peak(tmp_path, qapp):
    """BPSR's fresh install downloads the 40 GiB zip before it needs 85 GiB free."""
    from nadare.backends.direct import BpsrBackend

    dialog = AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data"
    )
    try:
        dialog.set_backend("direct-bpsr")
        text = dialog.size_hint.text()
        assert f"about {human_size(BpsrBackend.download_size_hint)}" in text
        assert f"about {human_size(BpsrBackend.install_size_hint)}" not in text
        assert f"about {human_size(BpsrBackend.install_space_hint)}" not in text
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_profile_name_defaults_to_title_and_is_unique(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    # Another game's profile already uses the default name; the name, not the catalog rule,
    # makes the dialog pick a free one.
    config.add_profile("arknights", backend="wine")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        assert dialog.name.text() == "arknights-2"
        existing = tmp_path / "somewhere"
        existing.mkdir()
        (existing / "Arknights.exe").write_text("MZ")
        dialog.set_install_dir(existing)
        assert dialog.create_profile() == "arknights-2"
        assert "arknights-2" in config.profiles
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_duplicate_explicit_name_is_rejected(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("mine", backend="stove-czn")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        dialog.set_profile_name("mine")
        assert dialog.create_profile() is None
        assert "already used" in dialog.error_label.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_install_new_does_not_create_the_folder(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("arknights-yostar")
        folder = Path(dialog.folder.text())
        dialog.create_profile()
        assert not folder.exists(), "the wizard must not create install folders"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_fgo_is_marked_experimental() -> None:
    from nadare.gui.add_game import CATALOG

    fgo = next(g for g in CATALOG if g.kind == "fgo-lepton")
    assert fgo.title == "Fate/Grand Order"  # the profile title stays clean
    assert fgo.badge == "Experimental"
    assert "binder" in fgo.badge_help


def test_a_fresh_install_hides_folder_and_prefix_until_asked(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.show()
        qapp.processEvents()
        dialog.set_backend("arknights-yostar")
        assert not dialog.folder.isVisible() and not dialog.prefix.isVisible()
        assert dialog.location_toggle.isVisible()
        dialog.location_toggle.click()
        assert dialog.folder.isVisible()
        dialog.location_toggle.click()
        assert not dialog.folder.isVisible()
        result = dialog.create_profile()  # the hidden defaults still apply
        assert result == "arknights", (result, dialog.error_label.text())
    finally:
        dialog.close()
        qapp.processEvents()


def test_a_second_fgo_copy_gets_its_own_android_context():
    """#921: every FGO profile without a context used the one named "fgo"."""
    from nadare.gui.add_game import unique_context

    assert unique_context({}) is None  # the first copy keeps the default name
    first = {"a": {"backend": "fgo-lepton"}}
    assert unique_context(first) == "fgo-2"
    both = {**first, "b": {"backend": "fgo-lepton", "context": "fgo-2"}}
    assert unique_context(both) == "fgo-3"
    other = {"w": {"backend": "wine"}}
    assert unique_context(other) is None  # only FGO profiles use contexts


def test_enter_in_a_text_field_does_not_create_the_game(tmp_path, qapp, monkeypatch):
    """Enter finishes typing a name; it used to add the (preselected) game at once."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: pytest.fail("Browse"))
    )
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config)
    try:
        dialog.show()
        qapp.processEvents()
        dialog.name.setFocus()
        dialog.name.setText("my-game")
        QTest.keyClick(dialog.name, Qt.Key.Key_Return)
        qapp.processEvents()
        assert config.profiles == {} and dialog.isVisible()
        dialog.create_button.setFocus()
        QTest.keyClick(dialog.create_button, Qt.Key.Key_Space)  # the button itself still works
        qapp.processEvents()
        assert len(config.profiles) == 1
    finally:
        dialog.close()
        dialog.deleteLater()


def test_an_error_is_shown_above_the_buttons_not_below_the_scrolled_form(tmp_path, qapp):
    """On a short screen the message sat under the form, out of view: Add game "did nothing"."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QScrollArea

    config = Config.load(tmp_path / "config.json")
    config.add_profile("taken", backend="wine", title="Taken")
    dialog = AddGameDialog(config)
    try:
        dialog.resize(560, 320)
        dialog.show()
        qapp.processEvents()
        dialog.name.setText("taken")
        dialog.create_profile()  # refused: the name is already used
        qapp.processEvents()
        assert dialog.error_label.text()
        scroll = dialog.findChild(QScrollArea)
        assert not scroll.isAncestorOf(dialog.error_label)
        top_left = dialog.error_label.mapTo(dialog, QPoint(0, 0))
        assert dialog.rect().contains(top_left) and dialog.error_label.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_no_empty_strip_above_the_buttons_while_there_is_no_error(tmp_path, qapp):
    dialog = AddGameDialog(Config.load(tmp_path / "config.json"))
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.error_label.isHidden()
    finally:
        dialog.close()
        dialog.deleteLater()
