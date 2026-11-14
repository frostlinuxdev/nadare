# SPDX-License-Identifier: GPL-3.0-or-later
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace

import pytest
from _parts_patch import patch_everywhere
from PyQt6.QtWidgets import QApplication

from nadare.core.config import Config
from nadare.gui import runners_dialog
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])

def test_manage_runners_snapshots_profiles_under_the_config_lock(tmp_path, qapp, monkeypatch):
    patch_everywhere(monkeypatch, settings_mod, "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="not_installed")
        ),
    )
    patch_everywhere(
        monkeypatch, settings_mod, "discover_runners", lambda kind="": [("Default", "")]
    )
    seen = []

    class DummyDialog:
        def __init__(self, profiles, parent):
            seen.append(profiles)
        def exec(self):
            return 0

    monkeypatch.setattr(runners_dialog, "ManageRunnersDialog", DummyDialog)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    page = SettingsPage()
    page.load(config, "sample")
    real_deepcopy = settings_mod.copy.deepcopy

    def racing_deepcopy(value):
        # A worker may only get in while the config lock is not held.
        if value is config.profiles and not config._lock._is_owned():
            raise RuntimeError("dictionary changed size during iteration")
        return real_deepcopy(value)

    monkeypatch.setattr(settings_mod.copy, "deepcopy", racing_deepcopy)
    try:
        page._open_runners_dialog()
        assert len(seen) == 1
        assert seen[0]["sample"] is not config.profiles["sample"]
    finally:
        page.deleteLater()
        qapp.processEvents()
