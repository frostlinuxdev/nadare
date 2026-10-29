# SPDX-License-Identifier: GPL-3.0-or-later
"""The relink dialog says what a hand relink does and asks about a game folder left behind."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage

YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


def _prefix(path: Path) -> Path:
    (path / "drive_c" / "windows").mkdir(parents=True)
    return path


class Rig:
    def __init__(
        self, tmp_path, qapp, monkeypatch, *, answers=(YES, YES), sibling=False, shared=False
    ):
        monkeypatch.setattr(
            settings_mod,
            "create_backend",
            lambda profile, config: SimpleNamespace(
                status=lambda: SimpleNamespace(state="installed", data={})
            ),
        )
        home = tmp_path / "lib" / "Nadare" / "Game"
        old = _prefix(home / "prefix")
        install = home / "install"
        install.mkdir()
        self.install = install
        self.new = tmp_path / "elsewhere" / "pfx"
        self.new.parent.mkdir()
        shutil.move(str(old), str(self.new))
        if sibling:
            self.moved_install = self.new.parent / "install"
            shutil.move(str(install), str(self.moved_install))
        self.config = Config.load(tmp_path / "config.json")
        self.config.add_profile(
            "game",
            backend="wine",
            title="Game",
            prefix=str(old),
            install_dir=str(install),
            exe_name="game.exe",
            owned_dirs=[str(home)],
            owned_trees=["install_dir", "prefix"],
            owned_paths={
                "install_dir": str(install.resolve()),
                "prefix": str(old.resolve()),
            },
        )
        if shared:
            self.config.add_profile(
                "other", backend="wine", title="Other Game", prefix=str(self.new)
            )
        self.config.save()
        self.questions: list[str] = []
        queue = list(answers)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(self.new))
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **k: self.questions.append(a[2]) or (queue.pop(0) if queue else YES),
        )
        self.shown: list[str] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: self.shown.append(a[2]))
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: self.shown.append(a[2]))
        self.page = SettingsPage()
        self.page.load(self.config, "game")
        _settle(qapp)
        self.qapp = qapp
        self.tmp = tmp_path

    def relink(self):
        self.page.relink_button.click()
        _settle(self.qapp)
        return Config.load(self.tmp / "config.json").profiles["game"]

    def close(self):
        self.page.deleteLater()
        self.qapp.processEvents()


@pytest.fixture
def make(tmp_path, qapp, monkeypatch):
    rigs: list[Rig] = []

    def build(**kw):
        rig = Rig(tmp_path, qapp, monkeypatch, **kw)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


def test_the_confirm_dialog_says_the_folder_will_not_be_deleted(make):
    rig = make()
    saved = rig.relink()
    assert saved["prefix"] == str(rig.new)
    assert "will not delete" in rig.questions[0]
    assert str(rig.new) in rig.questions[0]
    assert "prefix" not in saved["owned_trees"]


def test_a_game_folder_left_behind_is_mentioned_in_the_dialog(make):
    rig = make()
    rig.relink()
    assert str(rig.install) in rig.questions[0]


def test_a_prefix_another_game_uses_is_warned_about_in_the_dialog(make):
    rig = make(shared=True)
    rig.relink()
    assert "Other Game" in rig.questions[0] and "also used" in rig.questions[0]


def test_a_game_folder_next_to_the_new_prefix_is_offered_and_can_be_followed(make):
    rig = make(sibling=True, answers=(YES, YES))
    saved = rig.relink()
    assert len(rig.questions) == 2
    assert str(rig.moved_install) in rig.questions[1]
    assert saved["install_dir"] == str(rig.moved_install)
    assert "install_dir" not in saved["owned_trees"]


def test_declining_the_offered_game_folder_leaves_it_alone(make):
    rig = make(sibling=True, answers=(YES, NO))
    saved = rig.relink()
    assert len(rig.questions) == 2
    assert saved["install_dir"] == str(rig.install)
    assert saved["prefix"] == str(rig.new)


def test_no_second_question_when_there_is_no_game_folder_to_offer(make):
    rig = make()
    rig.relink()
    assert len(rig.questions) == 1


def test_declining_the_confirm_changes_nothing(make):
    rig = make(answers=(NO,))
    saved = rig.relink()
    assert saved["prefix"] != str(rig.new)
    assert "prefix" in saved["owned_trees"]
