# SPDX-License-Identifier: GPL-3.0-or-later
"""#1300 item 5 in the window: the other copies are offered once the chosen one works."""

from __future__ import annotations

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001 - PyQt6 may be missing
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_1300_spare_copies import _copy_folder

from nadare.core import gamefinder, libraries
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.found_copies import SpareCopiesDialog
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def case(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    monkeypatch.setattr(gamefinder, "_prefix_in_use", lambda prefix: False)
    base = tmp_path / "drive" / "Gacha games" / "Nadare"
    keep = _copy_folder(base, "Honkai Star Rail")
    spare = _copy_folder(base, "Honkai Star Rail old")
    old = base / "Nadare" / "Honkai Star Rail"
    config = Config.load(tmp_path / "config.json")
    config.data[libraries.CONFIG_KEY] = [{"path": str(base)}]
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
    return config, keep, spare


def _window_after_use(config, keep, monkeypatch, asked):
    # No real probe, from the start: open_game() used to start one before this patch, and a
    # worker that read the profile after the re-point reported "installed" on its own (a CI
    # flake on 3.12, PR #1641).
    monkeypatch.setattr(MainWindow, "_start", lambda self, *a, **k: None)
    window = MainWindow(config)
    window._show_found_copies({"hsr": gamefinder.find_copies(config, "hsr", None)})
    window.open_game("hsr")

    def choose(self, name, copies):
        return next(c for c in copies if c.game_dir == keep)

    monkeypatch.setattr(MainWindow, "_choose_found_copy", choose)
    monkeypatch.setattr(
        MainWindow, "_ask_spare_copies", lambda self, title, spares: asked.append(spares) or []
    )
    window._use_found_copy("hsr")
    return window


def test_installed_after_the_repoint_offers_the_other_copy(case, qapp, qtbot, monkeypatch):
    config, keep, spare = case
    asked: list = []
    window = _window_after_use(config, keep, monkeypatch, asked)
    window._on_result("hsr", "status", Status(state="installed"))
    qtbot.waitUntil(lambda: bool(asked), timeout=5000)
    assert {s.path for s in asked[0]} == {spare / "install", spare / "prefix"}
    window._on_result("hsr", "status", Status(state="installed"))  # only once
    qtbot.wait(200)
    assert len(asked) == 1
    window.close()


def test_no_offer_until_the_chosen_copy_works(case, qapp, qtbot, monkeypatch):
    config, keep, _spare = case
    asked: list = []
    window = _window_after_use(config, keep, monkeypatch, asked)
    window._on_result("hsr", "status", Status(state="not_installed"))
    window._on_result("hsr", "status", Status(state="installed", running=True))
    window._on_result("hsr", "status", Status(state="error", message="broken"))
    qtbot.wait(300)
    assert asked == []
    assert "hsr" in window._spare_pending
    window._on_result("hsr", "status", Status(state="installed"))
    qtbot.waitUntil(lambda: bool(asked), timeout=5000)
    window.close()


def test_ticked_folders_are_deleted_and_the_chosen_copy_stays(case, qapp, qtbot, monkeypatch):
    config, keep, spare = case
    asked: list = []
    window = _window_after_use(config, keep, monkeypatch, asked)
    monkeypatch.setattr(MainWindow, "_ask_spare_copies", lambda self, title, spares: spares)
    lines: list[str] = []
    monkeypatch.setattr(window, "_set_side_status", lines.append)
    window._on_result("hsr", "status", Status(state="installed"))
    qtbot.waitUntil(lambda: not spare.exists(), timeout=5000)
    # The page's own "Installed" line comes first: wait for the deletion's.
    qtbot.waitUntil(lambda: any("Deleted" in line for line in lines), timeout=5000)
    assert any("Deleted 2 folders" in line for line in lines)
    assert (keep / "install" / "StarRail.exe").is_file()
    window.close()


def test_a_game_with_one_copy_is_never_offered_anything(tmp_path, qapp, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    base = tmp_path / "drive" / "Nadare"
    keep = _copy_folder(base, "Honkai Star Rail")
    old = base / "Nadare" / "Honkai Star Rail"
    config = Config.load(tmp_path / "config.json")
    config.data[libraries.CONFIG_KEY] = [{"path": str(base)}]
    config.add_profile(
        "hsr",
        backend="direct-hsr",
        install_dir=str(old / "install"),
        prefix=str(old / "prefix"),
        owned_dirs=[str(old)],
        owned_trees=["install_dir", "prefix"],
    )
    asked: list = []
    window = _window_after_use(config, keep, monkeypatch, asked)
    assert "hsr" not in window._spare_pending
    window.close()


def test_the_dialog_ticks_nothing_and_keeps_by_default(case, qapp):
    config, keep, _spare = case
    copies = gamefinder.find_copies(config, "hsr", None)
    chosen = next(c for c in copies if c.game_dir == keep)
    gamefinder.use_and_save(config, "hsr", chosen)
    spares = gamefinder.spares(config, "hsr", copies, chosen)
    dialog = SpareCopiesDialog("Honkai: Star Rail", spares)
    try:
        assert [box.isChecked() for box in dialog.boxes] == [False, False]
        assert not dialog.delete_button.isEnabled()
        assert dialog.ticked() == []
        assert any("Wine prefix" in box.text() for box in dialog.boxes)
        dialog.boxes[0].setChecked(True)
        assert dialog.delete_button.isEnabled()
        assert dialog.ticked() == [spares[0]]
    finally:
        dialog.deleteLater()
