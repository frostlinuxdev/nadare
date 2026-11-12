# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229 in Settings: "Move into a library" for an import."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import gamefolders, libraries
from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage

YES, NO = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(10000)
    for _ in range(8):
        qapp.processEvents()


@pytest.fixture
def rig(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(
        settings_mod, "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="installed", data={})
        ),
    )
    shown: list[str] = []
    answers = {"value": YES}
    asked: list[str] = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or answers["value"]
    )
    library = tmp_path / "Games"
    library.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(library))
    config = Config.load(tmp_path / "config.json")
    install = tmp_path / "old" / "WW"
    (install / "Client").mkdir(parents=True)
    (install / "Client" / "g.pak").write_bytes(b"x" * 100)
    prefix = tmp_path / "old" / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    config.add_profile(
        "wuwa", backend="direct-wuwa", title="Wuthering Waves",
        install_dir=str(install), prefix=str(prefix),
    )
    config.save()
    page = SettingsPage()
    rig = SimpleNamespace(
        config=config, page=page, shown=shown, asked=asked, answers=answers,
        library=library, install=install, prefix=prefix,
    )
    yield rig
    page.deleteLater()
    qapp.processEvents()


def test_an_import_offers_move_into_a_library(rig, qapp):
    rig.page.load(rig.config, "wuwa")
    _settle(qapp)
    assert not rig.page.import_move_button.isHidden()


def test_a_library_game_does_not_offer_it(rig, qapp):
    home = rig.library / "Nadare" / "WW"
    (home / "install").mkdir(parents=True)
    (home / "prefix" / "drive_c").mkdir(parents=True)
    rig.config.profiles["wuwa"].update(
        install_dir=str(home / "install"), prefix=str(home / "prefix"),
        owned_dirs=[str(home)], owned_trees=["install_dir", "prefix"],
    )
    libraries.add_library(rig.config, rig.library)
    rig.config.save()
    rig.page.load(rig.config, "wuwa")
    _settle(qapp)
    assert rig.page.import_move_button.isHidden()


def test_moving_an_import_makes_it_a_library_game(rig, qapp):
    rig.page.load(rig.config, "wuwa")
    _settle(qapp)
    rig.page.import_move_button.click()
    _settle(qapp)
    assert rig.asked and "Uninstall can delete it" in rig.asked[0]
    saved = Config.load(rig.config.path)
    home = rig.library / "Nadare" / "Wuthering Waves"
    assert saved.profiles["wuwa"]["install_dir"] == str(home / "install")
    assert saved.profiles["wuwa"]["prefix"] == str(home / "prefix")
    assert (home / "install" / "Client" / "g.pak").is_file()
    assert not rig.install.exists()  # same disk: renamed, not copied
    place = gamefolders.placement(saved, "wuwa")
    assert place.made_by_nadare and place.library == rig.library.resolve()


def test_saying_no_moves_nothing(rig, qapp):
    rig.answers["value"] = NO
    rig.page.load(rig.config, "wuwa")
    _settle(qapp)
    rig.page.import_move_button.click()
    _settle(qapp)
    assert rig.install.is_dir() and rig.prefix.is_dir()
    assert Config.load(rig.config.path).profiles["wuwa"]["install_dir"] == str(rig.install)


def test_after_the_move_the_page_says_in_your_library(rig, qapp):
    rig.page.load(rig.config, "wuwa")
    _settle(qapp)
    rig.page.import_move_button.click()
    _settle(qapp)
    assert "In your library" in rig.page.place_label.text()
    assert rig.page.import_move_button.isHidden()
