# SPDX-License-Identifier: GPL-3.0-or-later
"""#1293 in the window: a game whose files sit elsewhere is offered back, never re-downloaded.

The support case: HSR's 129 GiB were at ``X/Nadare/Honkai Star Rail`` while the profile pointed at
``X/Nadare/Nadare/Honkai Star Rail``. The page said INSTALL (which would download it all again into
the old folder) and nothing in the app could point it back. Now the start-up check finds the copy,
a banner says so, the page's main button becomes "use the copy on disk", and one confirmation
re-points the profile. Nothing on disk moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001 - PyQt6 may be missing
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import gamefinder, libraries
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def case(tmp_path, monkeypatch):
    """The support case on a fake drive: HSR's profile points at the b21 folder, files are flat."""
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    drive = tmp_path / "drive" / "Gacha games"
    real = drive / "Nadare" / "Honkai Star Rail"
    (real / "install").mkdir(parents=True)
    (real / "install" / "StarRail.exe").write_text("exe")
    (real / "prefix" / "drive_c" / "windows").mkdir(parents=True)
    (real / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": libraries.MANIFEST_SCHEMA,
                "backend": "direct-hsr",
                "title": "Honkai: Star Rail",
                "install": "install",
                "prefix": "prefix",
                "made_by_nadare": ["install", "prefix"],
            }
        )
    )
    old = drive / "Nadare" / "Nadare" / "Honkai Star Rail"
    config = Config.load(tmp_path / "config.json")
    config.data[libraries.CONFIG_KEY] = [{"path": str(drive / "Nadare")}]
    config.add_profile(
        "hsr",
        backend="direct-hsr",
        title="Honkai: Star Rail",
        install_dir=str(old / "install"),
        prefix=str(old / "prefix"),
        owned_dirs=[str(old)],
        owned_trees=["install_dir", "prefix"],
    )
    config.save()
    return config, real.resolve(), old


def _found(config):
    from nadare.gui.add_game import game_specs

    return gamefinder.repairs(config, game_specs())


# ---- the game page --------------------------------------------------------------------------


def test_the_page_offers_the_copy_on_disk_instead_of_install(qapp):
    view = GameView()
    view.set_profile("hsr", {"title": "Honkai: Star Rail", "backend": "direct-hsr"})
    view.set_status(Status(state="not_installed", message="Not installed"))
    assert view.primary.text() == "INSTALL"
    view.set_found_copy("/drive/Gacha games/Nadare/Honkai Star Rail")
    assert view.primary.text() == "USE THE COPY ON DISK…"
    assert "/drive/Gacha games/Nadare/Honkai Star Rail" in view.detail_label.text()
    seen: list[str] = []
    ops: list[str] = []
    view.found_copy_requested.connect(seen.append)
    view.action_requested.connect(ops.append)
    view.primary.click()
    assert seen == ["hsr"]
    assert ops == []  # never an install
    view.set_found_copy(None)
    assert view.primary.text() == "INSTALL"
    view.deleteLater()


def test_an_installed_game_never_shows_the_offer(qapp):
    view = GameView()
    view.set_profile("hsr", {"title": "Honkai: Star Rail", "backend": "direct-hsr"})
    view.set_found_copy("/somewhere")
    view.set_status(Status(state="installed", message=""))
    assert view.primary.text() == "LAUNCH"
    view.deleteLater()


def test_switching_games_drops_the_offer(qapp):
    view = GameView()
    view.set_profile("hsr", {"title": "Honkai: Star Rail", "backend": "direct-hsr"})
    view.set_found_copy("/somewhere")
    view.set_profile("other", {"title": "Other", "backend": "direct-hsr"})
    view.set_status(Status(state="not_installed", message="Not installed"))
    assert view.primary.text() == "INSTALL"
    view.deleteLater()


# ---- the window -----------------------------------------------------------------------------


def test_the_start_check_finds_the_case_and_shows_a_banner(case, qapp):
    config, real, _old = case
    window = MainWindow(config)
    window._show_found_copies(_found(config))
    assert set(window._found_copies) == {"hsr"}
    assert not window.found_banner.isHidden()
    assert "Honkai: Star Rail" in window.found_banner.message_label.text()
    window.open_game("hsr")
    window.game.set_status(Status(state="not_installed", message="Not installed"))
    assert window.game.primary.text() == "USE THE COPY ON DISK…"
    assert str(real) in window.game.detail_label.text()
    window.close()


def test_nothing_found_shows_no_banner(tmp_path, qapp, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    config = Config.load(tmp_path / "config.json")
    window = MainWindow(config)
    window._show_found_copies({})
    assert window.found_banner.isHidden()
    window.close()


def test_the_real_start_check_runs_off_the_gui_thread_and_reports(case, qapp, qtbot):
    config, _real, _old = case
    window = MainWindow(config)
    window._real_start_found_copies_check()
    qtbot.waitUntil(lambda: "hsr" in window._found_copies, timeout=5000)
    assert not window.found_banner.isHidden()
    window.close()


def test_using_the_copy_repoints_the_profile_saves_and_moves_nothing(case, qapp, monkeypatch):
    config, real, old = case
    window = MainWindow(config)
    window._show_found_copies(_found(config))
    window.open_game("hsr")
    asked: list[list[Path]] = []

    def choose(self, name, copies):
        asked.append([c.game_dir for c in copies])
        return copies[0]

    monkeypatch.setattr(MainWindow, "_choose_found_copy", choose)
    window.game.found_copy_requested.emit("hsr")
    assert asked == [[real]]
    prof = config.profiles["hsr"]
    assert Path(prof["install_dir"]) == real / "install"
    assert Path(prof["prefix"]) == real / "prefix"
    # Saved to disk, not only in memory.
    on_disk = Config.load(config.path).profiles["hsr"]
    assert Path(on_disk["install_dir"]) == real / "install"
    # The old folder was not made, the copy was not moved.
    assert not old.exists()
    assert (real / "install" / "StarRail.exe").is_file()
    # The offer is gone: banner hidden, page back to the normal flow.
    assert "hsr" not in window._found_copies
    assert window.found_banner.isHidden()
    window.close()


def test_not_now_keeps_everything_and_brings_install_back(case, qapp, monkeypatch):
    config, _real, old = case
    window = MainWindow(config)
    # After start-up: its library-entry repair is not what this test is about.
    before = json.dumps(config.data, sort_keys=True)
    window._show_found_copies(_found(config))
    window.open_game("hsr")
    monkeypatch.setattr(MainWindow, "_choose_found_copy", lambda self, name, copies: "ignore")
    window.game.found_copy_requested.emit("hsr")
    assert json.dumps(config.data, sort_keys=True) == before
    assert "hsr" not in window._found_copies
    window.game.set_status(Status(state="not_installed", message="Not installed"))
    assert window.game.primary.text() == "INSTALL"
    assert not old.exists()
    window.close()


def test_cancel_keeps_the_offer(case, qapp, monkeypatch):
    config, _real, _old = case
    window = MainWindow(config)
    # After start-up: its library-entry repair is not what this test is about.
    before = json.dumps(config.data, sort_keys=True)
    window._show_found_copies(_found(config))
    window.open_game("hsr")
    monkeypatch.setattr(MainWindow, "_choose_found_copy", lambda self, name, copies: None)
    window.game.found_copy_requested.emit("hsr")
    assert json.dumps(config.data, sort_keys=True) == before
    assert "hsr" in window._found_copies
    window.close()


def test_a_refused_copy_says_why_and_changes_nothing(case, qapp, monkeypatch):
    config, real, _old = case
    window = MainWindow(config)
    window._show_found_copies(_found(config))
    window.open_game("hsr")
    monkeypatch.setattr(MainWindow, "_choose_found_copy", lambda self, name, copies: copies[0])
    # The folder went away between the scan and the click.
    real.rename(real.parent / "gone")
    before = json.dumps(config.data, sort_keys=True)
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(str(a[2])))
    )
    window.game.found_copy_requested.emit("hsr")
    assert len(warned) == 1
    assert json.dumps(config.data, sort_keys=True) == before
    window.close()


def test_a_failed_save_puts_the_profile_back(case, qapp, monkeypatch):
    config, _real, _old = case
    before = json.dumps(config.profiles["hsr"], sort_keys=True)
    window = MainWindow(config)
    window._show_found_copies(_found(config))
    window.open_game("hsr")
    monkeypatch.setattr(MainWindow, "_choose_found_copy", lambda self, name, copies: copies[0])

    def broken_save(self):
        raise OSError("disk full")

    monkeypatch.setattr(Config, "save", broken_save)
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(str(a[2])))
    )
    window.game.found_copy_requested.emit("hsr")
    assert len(shown) == 1 and "disk full" in shown[0]
    assert json.dumps(config.profiles["hsr"], sort_keys=True) == before
    assert "hsr" in window._found_copies
    window.close()


def test_the_banner_review_opens_the_game_and_asks(case, qapp, monkeypatch):
    config, real, _old = case
    window = MainWindow(config)
    window._show_found_copies(_found(config))
    asked: list[str] = []
    monkeypatch.setattr(
        MainWindow, "_choose_found_copy", lambda self, name, copies: asked.append(name)
    )
    window.found_banner.action_clicked.emit()
    assert asked == ["hsr"]
    assert window._current_profile == "hsr"
    window.close()


def test_the_chooser_lists_every_copy_best_first(case, qapp):
    """The real dialog: one row per copy with its folder; the best one is selected."""
    from nadare.gui.found_copies import FoundCopiesDialog

    config, real, _old = case
    copies = _found(config)["hsr"]
    dialog = FoundCopiesDialog("Honkai: Star Rail", copies)
    assert dialog.list.count() == len(copies)
    assert dialog.list.currentRow() == 0
    assert str(real) in dialog.list.item(0).text()
    assert dialog.chosen() == copies[0]
    dialog.deleteLater()


def test_the_chooser_keeps_version_and_completeness_visible_for_long_paths(case, qapp):
    """Seen on the b24 AppImage: a long folder pushed "· complete" off the right edge."""
    from PyQt6.QtCore import Qt

    from nadare.gui.found_copies import FoundCopiesDialog

    config, real, _old = case
    copies = _found(config)["hsr"]
    dialog = FoundCopiesDialog("Honkai: Star Rail", copies)
    item = dialog.list.item(0)
    first, second = item.text().split("\n")
    assert first == str(real)
    assert "complete" in second
    assert item.toolTip() == str(real)
    assert dialog.list.textElideMode() == Qt.TextElideMode.ElideMiddle
    assert dialog.list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    dialog.deleteLater()
