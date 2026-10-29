# SPDX-License-Identifier: GPL-3.0-or-later
"""The Settings Move keeps ``Nadare/<Game>/{install,prefix}`` and says what moves and what stays."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


def _make(lib: Path, name: str = "Game", *, managed: bool = True) -> dict:
    home = lib / "Nadare" / name if managed else lib / name
    prefix = home / "prefix"
    (prefix / "drive_c" / "windows").mkdir(parents=True)
    install = home / "install"
    install.mkdir()
    (install / "game.exe").write_bytes(b"MZ" * 50)
    profile = {
        "backend": "wine",
        "title": name,
        "exe_name": "game.exe",
        "prefix": str(prefix),
        "install_dir": str(install),
    }
    if managed:
        profile.update(
            owned_dirs=[str(home)],
            owned_trees=["install_dir", "prefix"],
            owned_paths={
                "install_dir": str(install.resolve()),
                "prefix": str(prefix.resolve()),
            },
        )
    return profile


class Rig:
    def __init__(self, tmp_path, qapp, monkeypatch, picks, profiles):
        monkeypatch.setattr(
            settings_mod,
            "create_backend",
            lambda profile, config: SimpleNamespace(
                status=lambda: SimpleNamespace(state="installed", data={})
            ),
        )
        self.tmp, self.qapp = tmp_path, qapp
        self.config = Config.load(tmp_path / "config.json")
        for name, profile in profiles.items():
            self.config.add_profile(name, **profile)
        self.config.save()
        self.picks = list(picks)
        self.questions: list[str] = []
        self.shown: list[str] = []
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *a, **k: str(self.picks.pop(0))
        )
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: self.questions.append(a[2]) or YES
        )
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: self.shown.append(a[2]))
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: self.shown.append(a[2]))
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: self.shown.append(a[2]))
        self.page = SettingsPage()

    def move(self, name: str) -> dict:
        self.page.load(self.config, name)
        _settle(self.qapp)
        self.page.move_button.click()
        _settle(self.qapp)
        return Config.load(self.tmp / "config.json").profiles[name]

    def close(self):
        self.page.deleteLater()
        self.qapp.processEvents()


@pytest.fixture
def make(tmp_path, qapp, monkeypatch):
    rigs: list[Rig] = []

    def build(picks, profiles):
        rig = Rig(tmp_path, qapp, monkeypatch, picks, profiles)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


def test_a_move_keeps_the_nadare_game_layout_and_moves_both_folders(make, tmp_path):
    rig = make([tmp_path / "big"], {"game": _make(tmp_path / "lib")})
    saved = rig.move("game")
    new_home = tmp_path / "big" / "Nadare" / "Game"
    assert saved["prefix"] == str(new_home / "prefix")
    assert saved["install_dir"] == str(new_home / "install")
    assert saved["owned_dirs"] == [str(new_home)]
    assert (new_home / "prefix" / "drive_c").is_dir()
    assert (new_home / "install" / "game.exe").is_file()
    assert not (tmp_path / "lib" / "Nadare" / "Game").exists()
    assert "game folder" in rig.questions[0].lower() and "prefix" in rig.questions[0].lower()
    assert str(new_home) in rig.questions[0]


def test_two_games_move_into_the_same_library(make, tmp_path):
    profiles = {
        "alpha": _make(tmp_path / "lib", "Alpha"),
        "beta": _make(tmp_path / "lib", "Beta"),
    }
    rig = make([tmp_path / "big", tmp_path / "big"], profiles)
    first = rig.move("alpha")
    second = rig.move("beta")
    assert first["prefix"] == str(tmp_path / "big" / "Nadare" / "Alpha" / "prefix")
    assert second["prefix"] == str(tmp_path / "big" / "Nadare" / "Beta" / "prefix")
    assert not any("already exists" in text for text in rig.shown)


def test_a_game_nadare_did_not_lay_out_moves_only_its_prefix_and_says_so(make, tmp_path):
    profile = _make(tmp_path / "mine", managed=False)
    rig = make([tmp_path / "big"], {"game": profile})
    saved = rig.move("game")
    assert saved["prefix"] == str(tmp_path / "big" / "prefix")
    assert saved["install_dir"] == profile["install_dir"]
    assert "stays" in rig.questions[0].lower() and profile["install_dir"] in rig.questions[0]


def test_picking_the_library_it_already_lives_in_is_refused_clearly(make, tmp_path):
    profile = _make(tmp_path / "lib")
    rig = make([tmp_path / "lib"], {"game": profile})
    saved = rig.move("game")
    assert saved["prefix"] == profile["prefix"]
    assert rig.questions == []
    assert any("already" in text.lower() for text in rig.shown)


def test_a_symlinked_prefix_is_refused_before_anything_is_asked(make, tmp_path):
    real = tmp_path / "real"
    (real / "drive_c").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    rig = make([tmp_path / "big"], {"game": {"backend": "wine", "title": "G", "prefix": str(link)}})
    saved = rig.move("game")
    assert saved["prefix"] == str(link)
    assert rig.questions == [] and any("link" in text.lower() for text in rig.shown)
    assert rig.picks, "the folder dialog was never opened"


def test_a_game_kept_inside_this_games_folder_stops_the_move_before_anything_is_asked(
    make, tmp_path
):
    outer = _make(tmp_path / "lib", "Outer")
    inner = _make(tmp_path / "lib" / "Nadare" / "Outer", "Inner")
    inner["title"] = "Inner Game"
    rig = make([tmp_path / "big"], {"outer": outer, "inner": inner})
    saved = rig.move("outer")
    assert saved["prefix"] == outer["prefix"]
    assert rig.questions == []
    assert any("Inner Game" in text for text in rig.shown)
    assert (tmp_path / "lib" / "Nadare" / "Outer" / "Nadare" / "Inner" / "prefix").is_dir()


def test_a_missing_game_folder_never_leaves_a_half_moved_profile(make, tmp_path):
    """The review's case: the game folder is gone, so the home must not move and then fail."""
    import shutil

    profile = _make(tmp_path / "lib")
    shutil.rmtree(profile["install_dir"])
    rig = make([tmp_path / "big"], {"game": profile})
    saved = rig.move("game")
    assert saved["prefix"] == str(tmp_path / "big" / "prefix")  # the prefix-only move
    assert Path(saved["prefix"]).is_dir() and not Path(profile["prefix"]).exists()
    assert not any("did not arrive" in text for text in rig.shown)
    assert "stays" in rig.questions[0].lower()
