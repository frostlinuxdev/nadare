# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: the Monitor dropdown per game and the launcher-wide default monitor (#779)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QLabel
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import monitors
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _two_monitors(tmp_path, monkeypatch):
    root = tmp_path / "drm"
    for entry, mode in (("card0-DP-1", "2560x1440"), ("card0-DP-2", "1920x1080")):
        (root / entry).mkdir(parents=True)
        (root / entry / "status").write_text("connected\n")
        (root / entry / "modes").write_text(mode + "\n")
    monkeypatch.setattr(monitors, "DRM_ROOT", root)


@pytest.fixture
def page(qapp):
    widget = SettingsPage()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _config(tmp_path, **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", **profile)
    return config


def _items(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_the_dropdown_lists_auto_and_the_connected_monitors(tmp_path, page):
    page.load(_config(tmp_path), "sample")
    assert _items(page.option_widget("monitor")) == [
        ("Auto", ""),
        ("DP-1 (2560x1440)", "DP-1"),
        ("DP-2 (1920x1080)", "DP-2"),
    ]


def test_a_saved_monitor_is_selected_and_changing_it_saves(tmp_path, page):
    config = _config(tmp_path, monitor="DP-2")
    page.load(config, "sample")
    combo = page.option_widget("monitor")
    assert combo.currentData() == "DP-2"
    combo.setCurrentIndex(combo.findData("DP-1"))
    page.save()
    assert config.profile("sample")["monitor"] == "DP-1"


def test_choosing_auto_clears_the_saved_monitor(tmp_path, page):
    config = _config(tmp_path, monitor="DP-2")
    page.load(config, "sample")
    combo = page.option_widget("monitor")
    combo.setCurrentIndex(combo.findData(""))
    page.save()
    assert not config.profile("sample").get("monitor")


def test_a_monitor_missing_on_this_pc_is_shown_and_kept(tmp_path, page):
    config = _config(tmp_path, monitor="HDMI-A-1")
    page.load(config, "sample")
    combo = page.option_widget("monitor")
    assert combo.currentData() == "HDMI-A-1"
    assert "not connected" in combo.currentText()
    page.save()
    assert config.profile("sample")["monitor"] == "HDMI-A-1"


def test_leaving_the_monitor_alone_is_not_an_unsaved_change(tmp_path, page):
    page.load(_config(tmp_path, monitor="DP-2"), "sample")
    assert page._has_unsaved_changes() is False


def test_the_launcher_default_monitor_loads_and_saves(tmp_path, page):
    config = _config(tmp_path)
    config.data["default_monitor"] = "DP-1"
    page.load(config, "sample")
    combo = page.default_monitor
    assert [data for _text, data in _items(combo)] == ["", "DP-1", "DP-2"]
    assert combo.currentData() == "DP-1"
    combo.setCurrentIndex(combo.findData("DP-2"))
    page.save()
    assert config.data["default_monitor"] == "DP-2"
    combo.setCurrentIndex(combo.findData(""))
    page.save()
    assert not config.data.get("default_monitor")


def test_changing_the_default_monitor_counts_as_an_unsaved_change(tmp_path, page):
    config = _config(tmp_path)
    page.load(config, "sample")
    assert page._has_unsaved_changes() is False
    page.default_monitor.setCurrentIndex(page.default_monitor.findData("DP-1"))
    assert page._has_unsaved_changes() is True


def test_the_launcher_default_monitor_says_it_applies_to_all_games(page):
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "Monitor for all games" in labels
    assert "Default monitor" not in labels
    hint = page.default_monitor_hint.text()
    assert "all games" in hint and "own Monitor setting wins" in hint
    assert "Steam games" in hint
