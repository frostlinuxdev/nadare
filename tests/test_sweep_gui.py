# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for sidebar status, stale worker results and removal preflight."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import paths, removal
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.sidebar import SidebarGames


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="wine", title="A")
    config.add_profile("keep", backend="wine", title="Keep")
    return MainWindow(config)


# ---- #98: re-added profile must not reuse a removed profile's status ----------
def test_sidebar_drops_statuses_of_removed_profiles(qapp):
    sidebar = SidebarGames()
    profiles = {"a": {"title": "A"}, "b": {"title": "B"}}
    try:
        sidebar.set_profiles(
            profiles, {"a": Status("running", running=True), "b": Status("installed")}
        )
        assert sidebar.profiles() == ["a", "b"]
        assert not sidebar.row("a").dot.isHidden()

        # Removed with statuses=None: the stale "running" status must be dropped as well,
        # so the re-added game neither keeps its running rank nor its running dot.
        sidebar.set_profiles({"b": profiles["b"]})
        sidebar.set_profiles(profiles)
        assert sidebar.profiles() == ["b", "a"]
        assert sidebar.row("a").dot.isHidden()
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


# ---- #252: a worker result for the old profile must not hit the new one ------
def test_stale_worker_result_is_ignored_after_remove_and_readd(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    try:
        # The worker started while the original "a" owned the profile.
        assert window._start("a", "status", silent=True)

        window.config.remove_profile("a")
        window.refresh_library()
        window.config.add_profile("a", backend="wine", title="A")
        window.refresh_library()

        window._on_result("a", Status("installed"))
        assert "status" not in window._snapshots.get("a", {})
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #280: a config that cannot be saved must be noticed before deletion -----
def test_remove_game_keeps_data_when_config_save_fails(tmp_path, monkeypatch):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("keep", backend="wine", title="Keep")
    cfg.add_profile("gone", backend="wine", title="Gone")
    cfg.save()

    data = paths.profiles_dir() / "gone"
    data.mkdir(parents=True)
    (data / "save.dat").write_text("x")

    def fail_save():
        raise PermissionError("read-only config dir")

    monkeypatch.setattr(cfg, "save", fail_save)
    result = removal.remove_game(cfg, "gone", delete_data=True, applications_dir=tmp_path / "apps")

    assert data.exists() and (data / "save.dat").exists()
    assert result.profile_kept
    assert "gone" in cfg.profiles
    assert any("save" in warning.lower() for warning in result.warnings)
