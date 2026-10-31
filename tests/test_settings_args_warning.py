# SPDX-License-Identifier: GPL-3.0-or-later
"""The Launch arguments field warns when it holds Steam-style environment text (#779)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.settings import SettingsPage, args_env_warning


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.mark.parametrize(
    "text",
    [
        "WAYLANDDRV_PRIMARY_MONITOR=DP-2 %command%",
        "%command%",
        "-windowed WAYLANDDRV_PRIMARY_MONITOR=DP-1",
        "DXVK_HUD=fps",
    ],
)
def test_environment_text_in_launch_arguments_is_flagged(text):
    message = args_env_warning(text)
    assert message and "Environment" in message


@pytest.mark.parametrize(
    "text",
    [
        "",
        "-windowed -popupwindow",
        "--width=1920 --height=1080",
        "-screen-width 1920",
        "--config=/some/path.ini",
        "-logFile=a=b",
        "width=1920",
        "'unclosed",
    ],
)
def test_normal_launch_arguments_are_not_flagged(text):
    assert args_env_warning(text) is None


def test_saving_a_flagged_argument_still_saves_but_tells_the_user(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        text = "WAYLANDDRV_PRIMARY_MONITOR=DP-2 %command%"
        page.option_widget("launch_args").setText(text)
        page.save()
        assert config.profile("sample")["launch_args"] == text
        assert "Environment" in page.options_note.text()
    finally:
        page.deleteLater()
        qapp.processEvents()
