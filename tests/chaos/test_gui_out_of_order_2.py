# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""An unknown/running install state must not unlock Change location."""

from __future__ import annotations

from _parts_patch import patch_everywhere

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


def test_change_location_stays_locked_when_the_status_probe_fails(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    home = tmp_path / "lib" / "Nadare" / "Game"
    (home / "install").mkdir(parents=True)
    (home / "prefix" / "drive_c").mkdir(parents=True)
    config.add_profile(
        "g",
        backend="direct-hsr",
        title="G",
        install_dir=str(home / "install"),
        prefix=str(home / "prefix"),
        owned_dirs=[str(home)],
        owned_trees=["install_dir", "prefix"],
    )
    config.save()

    class Unreadable:
        def status(self):
            raise OSError("cannot read the process list")

    patch_everywhere(monkeypatch, settings_mod, "create_backend", lambda profile, cfg: Unreadable())
    page = SettingsPage()
    try:
        page.show()
        page.load(config, "g")
        from PyQt6.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(5000)
        for _ in range(8):
            qapp.processEvents()
        print("confirmed idle:", page._game_idle, "active:", page._game_active)
        print("Change location hidden:", page.change_location_button.isHidden())
        print("Change location enabled:", page.change_location_button.isEnabled())
        print("lock note:", page._prefix_lock_note.text())
        assert page.change_location_button.isHidden(), (
            "Change location is offered although Nadare could not tell whether the game runs"
        )
        assert not page.change_location_button.isEnabled()
    finally:
        page.deleteLater()
        qapp.processEvents()
