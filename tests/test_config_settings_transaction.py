# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings drafts persist before publishing to live backend holders."""

import copy
import json
import os

import pytest

from nadare.core.backends import BackendContext
from nadare.core.config import Config


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("game", backend="wine", title="Original", extra={
        "nested": {"remove": 1, "local": 0, "remote": 0}, "unknown": 42,
    })
    cfg.data["default_monitor"] = "old"
    cfg.save()
    return cfg


@pytest.mark.parametrize("failure", ["save", "replace", "fsync"])
def test_failure_never_publishes_draft_or_disk_merge(config, monkeypatch, failure):
    holder = BackendContext("game", config.profile("game"), config)
    data, profiles, extra = config.data, config.profiles, holder.profile["extra"]
    before = copy.deepcopy(data)
    snapshot = copy.deepcopy(config._snapshot)
    other = Config.load(config.path)
    other.profile("game")["remote_only"] = True
    other.data["disk_only"] = 9
    other.save()
    disk = config.path.read_bytes()

    def broken(*args, **kwargs):
        assert config.data == before  # even during the attempted write
        assert holder.profile["title"] == "Original"
        raise OSError("injected failure")

    if failure == "save":
        monkeypatch.setattr(Config, "save", broken)
    elif failure == "replace":
        monkeypatch.setattr("nadare.core.config.os.replace", broken)
    else:
        monkeypatch.setattr("nadare.core.config.os.fsync", broken)
    with pytest.raises(OSError, match="injected failure"):
        with config.edit_settings("game") as draft:
            draft.settings.pop("default_monitor")
            draft.settings["language"] = "de"
            draft.profile["title"] = "Edited"
            del draft.profile["extra"]["nested"]["remove"]
    assert config.data is data and config.profiles is profiles
    assert config.profile("game") is holder.profile
    assert holder.profile["extra"] is extra
    assert config.data == before and config._snapshot == snapshot
    assert config.path.read_bytes() == disk
    monkeypatch.undo()
    with config.edit_settings("game") as draft:
        draft.profile["title"] = "Retried"
    assert holder.profile["title"] == "Retried"
    assert holder.profile["remote_only"] is True
    assert config.data["disk_only"] == 9


def test_success_merges_nested_edits_removals_and_preserves_holders(config):
    holder = BackendContext("game", config.profile("game"), config)
    data, profiles = config.data, config.profiles
    extra = holder.profile["extra"]
    nested = extra["nested"]
    with config.edit_settings("game") as draft:
        draft.settings.pop("default_monitor")
        draft.settings["language"] = "de"
        draft.profile["title"] = "Edited"
        draft.profile["extra"]["nested"].pop("remove")
        draft.profile["extra"]["nested"]["local"] = 1
        other = Config.load(config.path)
        other.profile("game")["extra"]["nested"].update(remote=2, remove=99, new=3)
        other.profile("game")["unknown_disk"] = {"keep": True}
        other.data["disk_only"] = 7
        other.data["check_notices"] = False
        other.save()
        assert holder.profile["title"] == "Original"
    assert config.data is data and config.profiles is profiles
    assert config.profile("game") is holder.profile
    assert holder.profile["extra"] is extra and extra["nested"] is nested
    assert nested == {"local": 1, "remote": 2, "new": 3}
    assert extra["unknown"] == 42
    assert holder.profile["unknown_disk"] == {"keep": True}
    assert "default_monitor" not in data
    assert data["language"] == "de" and data["disk_only"] == 7
    assert data["check_notices"] is False
    assert json.loads(config.path.read_text()) == data
    draft.profile["title"] = "Changed after commit"
    assert holder.profile["title"] == "Edited"


def test_app_draft_and_aborted_edit(config):
    before = copy.deepcopy(config.data)
    with pytest.raises(RuntimeError):
        with config.edit_settings() as draft:
            assert draft.profile is None
            draft.settings["language"] = "de"
            raise RuntimeError("cancel")
    assert config.data == before
    with config.edit_settings() as draft:
        draft.settings["language"] = "de"
    assert Config.load(config.path).data["language"] == "de"


@pytest.mark.parametrize("invalid", ["profiles", "value", "boolean"])
def test_invalid_draft_is_rejected_before_write(config, invalid):
    before = copy.deepcopy(config.data)
    disk = config.path.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        with config.edit_settings("game") as draft:
            if invalid == "profiles":
                draft.settings["profiles"] = {}
            elif invalid == "boolean":
                draft.settings["check_updates"] = "false"
            else:
                draft.profile["extra"]["bad"] = object()
    assert config.data == before and config.path.read_bytes() == disk


def test_old_alias_config_saved_through_draft_keeps_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"future": {"keep": [1, 2]}, "profiles": {
        "old": {"backend": "wine", "proton_runner": "old-runner",
                "wineprefix": str(tmp_path / "prefix"), "future_profile": 3,
                "extra": {"future_nested": {"keep": 4}}},
    }}))
    cfg = Config.load(path)
    with cfg.edit_settings("old") as draft:
        draft.profile["title"] = "Saved"
    again = Config.load(path)
    assert again.profile("old")["runner"] == "old-runner"
    assert again.profile("old")["prefix"] == str(tmp_path / "prefix")
    assert again.profile("old")["future_profile"] == 3
    assert again.profile("old")["extra"]["future_nested"] == {"keep": 4}
    assert again.data["future"] == {"keep": [1, 2]}


@pytest.mark.parametrize("app_only", [True, False])
def test_settings_form_keeps_live_state_private_until_replace(config, monkeypatch, app_only):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from nadare.gui.settings import SettingsPage

    app = QApplication.instance() or QApplication([])
    page = SettingsPage()
    holder = BackendContext("game", config.profile("game"), config)
    before = copy.deepcopy(config.data)
    shown, saved = [], []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: shown.append(args[2]))
    try:
        if app_only:
            page.load_app_only(config)
        else:
            page.load(config, "game")
            page.title.setText("Edited")
        page.check_updates.setChecked(False)
        page.saved.connect(lambda *args: saved.append(args))

        def broken(*args):
            assert config.data == before
            assert holder.profile["title"] == "Original"
            raise OSError("injected replace failure")

        monkeypatch.setattr("nadare.core.config.os.replace", broken)
        page.save()
        assert config.data == before
        assert shown and "injected replace failure" in shown[0]
        assert not saved and page._has_unsaved_changes()
    finally:
        page.deleteLater()
        app.processEvents()


def test_draft_save_preserves_symlink_and_publishes_only_after_replace(config, monkeypatch):
    link = config.path.with_name("link.json")
    link.symlink_to(config.path)
    linked = Config.load(link)
    holder = BackendContext("game", linked.profile("game"), linked)
    replace = os.replace
    observed = []

    def check_then_replace(source, target):
        observed.append(holder.profile["title"])
        assert target == config.path
        replace(source, target)

    monkeypatch.setattr("nadare.core.config.os.replace", check_then_replace)
    with linked.edit_settings("game") as draft:
        draft.profile["title"] = "Committed"
    assert observed == ["Original"]
    assert link.is_symlink()
    assert holder.profile["title"] == "Committed"
    assert Config.load(config.path).profile("game")["title"] == "Committed"
