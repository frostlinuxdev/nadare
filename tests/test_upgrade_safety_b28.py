# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade safety regressions."""

import copy
import json

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import owngames
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_retirement_backup_failure_preserves_old_profiles(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "profiles": {
        "mine": {"backend": "wine", "exe_name": "game.exe"},
        "czn": {"backend": "stove-czn"},
    }}))
    before = path.read_bytes()
    cfg = Config.load(path)

    def cannot_copy(*args, **kwargs):
        raise OSError("backup storage unavailable")

    monkeypatch.setattr(owngames.shutil, "copy2", cannot_copy)
    from nadare.gui import _startup_notes

    live = copy.deepcopy(cfg.data)
    removed, saves = [], []
    monkeypatch.setattr(owngames, "_remove_shortcuts", lambda *args: removed.append(args))
    monkeypatch.setattr(Config, "save", lambda self: saves.append(self))
    notes = _startup_notes(cfg)
    assert notes == [
        "Nadare could not back up your settings, so it left your old games as they were: "
        "backup storage unavailable"
    ]
    assert cfg.data == live
    assert removed == []
    assert saves == []

    assert path.read_bytes() == before, "retirement rewrote the only copy of the old config"
    assert "mine" in cfg.profiles
    assert not cfg.data.get(owngames.RETIRED_FLAG)


@pytest.mark.parametrize("missing_key", ["check_holds", "download_speed_limit"])
def test_unrelated_settings_save_preserves_missing_new_keys(tmp_path, qapp, missing_key):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.save()
    old = json.loads(path.read_text())
    for key in ("check_holds", "download_speed_limit"):
        old.pop(key, None)  # These fields did not exist in the public release.
    path.write_text(json.dumps(old))
    cfg = Config.load(path)
    page = SettingsPage()
    try:
        page.load_app_only(cfg)
        page.notify_when_done.setChecked(not page.notify_when_done.isChecked())
        page.save()
        saved = json.loads(path.read_text())
        assert missing_key not in saved
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_game_speed_editor_loads_saved_global_limit(tmp_path, qapp):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("wuwa", backend="direct-wuwa")
    cfg.data["download_speed_limit"] = 7.5
    page = SettingsPage()
    try:
        page.load(cfg, "wuwa")
        assert page.download_speed_limit.value() == 7.5
        assert page._speed_editors, "fixture must exercise the game-side speed control"
        assert all(editor.value() == 7.5 for editor in page._speed_editors)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_retirement_save_failure_preserves_live_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "profiles": {
        "mine": {"backend": "wine"}, "czn": {"backend": "stove-czn"},
    }, "active_profile": "mine"}))
    before = path.read_bytes()
    cfg = Config.load(path)
    live = copy.deepcopy(cfg.data)
    snapshots = copy.deepcopy((cfg._snapshot, cfg._top_snapshot, cfg._removed))
    profile = cfg.profile("mine")
    removed = []

    def cannot_replace(*args):
        raise OSError("settings storage unavailable")

    monkeypatch.setattr("nadare.core.config.os.replace", cannot_replace)
    monkeypatch.setattr(owngames, "_remove_shortcuts", lambda *args: removed.append(args))
    with pytest.raises(OSError, match="settings storage unavailable"):
        owngames.retire(cfg)
    assert cfg.data == live
    assert cfg.profile("mine") is profile
    assert (cfg._snapshot, cfg._top_snapshot, cfg._removed) == snapshots
    assert path.read_bytes() == before
    assert removed == []
    assert list(tmp_path.glob("config.json.before-own-games-removed*"))[0].read_bytes() == before


@pytest.mark.parametrize("app_only", [True, False])
def test_settings_save_preserves_missing_profile_hold(tmp_path, qapp, app_only):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "profiles": {
        "wuwa": {"backend": "direct-wuwa"},
    }}))
    cfg = Config.load(path)
    page = SettingsPage()
    try:
        if app_only:
            page.load_app_only(cfg)
        else:
            page.load(cfg, "wuwa")
        assert page.download_speed_limit.value() == 0
        assert page.check_holds.isChecked()
        page.notify_when_done.setChecked(not page.notify_when_done.isChecked())
        page.save()
        saved = json.loads(path.read_text())
        assert "hold_override" not in saved["profiles"]["wuwa"]
        assert "download_speed_limit" not in saved
        assert "check_holds" not in saved
    finally:
        page.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("app_only", [True, False])
def test_settings_edits_optional_values_and_keeps_explicit_defaults(tmp_path, qapp, app_only):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema": 1, "profiles": {
        "wuwa": {"backend": "direct-wuwa", "hold_override": "accepted-hold"},
    }}))
    cfg = Config.load(path)
    page = SettingsPage()
    try:
        for speed, holds in [(7.5, False), (0, True)]:
            if app_only:
                page.load_app_only(cfg)
            else:
                page.load(cfg, "wuwa")
            page.download_speed_limit.setValue(speed)
            page.check_holds.setChecked(holds)
            page.save()
            saved = json.loads(path.read_text())
            assert saved["download_speed_limit"] == speed
            assert saved["check_holds"] is holds
            assert saved["profiles"]["wuwa"]["hold_override"] == "accepted-hold"
            cfg = Config.load(path)
        page.notify_when_done.setChecked(not page.notify_when_done.isChecked())
        page.save()
        saved = json.loads(path.read_text())
        assert saved["download_speed_limit"] == 0
        assert saved["check_holds"] is True
    finally:
        page.deleteLater()
        qapp.processEvents()
