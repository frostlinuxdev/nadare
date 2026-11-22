# SPDX-License-Identifier: GPL-3.0-or-later
"""Malformed imported settings and plain env text must not crash the editor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _parts_patch import patch_everywhere
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QMessageBox

from nadare.core.config import Config
from nadare.gui import settings as settings_module
from nadare.gui.settings import SettingsPage, _text_to_env


@pytest.mark.parametrize(
    "key,value",
    [
        ("extra_env", []),
        ("extra_env", 17),
        ("extra_env", {"A": [1, 2]}),
        ("extra_env", {"A": None}),
        ("launch_args", 42),
        ("launch_args", ["space name", "--flag"]),
        ("install_dir", {}),
        ("install_dir", 42),
        ("prefix", []),
        ("runner", {}),
        ("extra", []),
        ("extra", {"unknown_future": {"keep": [1, 2]}}),
    ],
)
def test_imported_profile_values_can_be_opened_without_traceback(
    tmp_path, qtbot, monkeypatch, key, value
):
    patch_everywhere(
        monkeypatch,
        settings_module,
        "create_backend",
        lambda *a: SimpleNamespace(status=lambda: SimpleNamespace(state="not_installed", data={})),
    )
    for name in ["warning", "information", "critical"]:
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: None)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine", title="Game", **{key: value})
    cfg.save()
    page = SettingsPage()
    try:
        page.load(cfg, "g")
        QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(1)
        assert page._profile == "g"
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
        page.deleteLater()
        qtbot.wait(1)


@pytest.mark.parametrize(
    "text",
    [
        "BAD-NAME=1\nGOOD=2",
        "1BAD=1\nGOOD=2",
        "BAD NAME=1\nGOOD=2",
        "missing equals\nGOOD=2",
        "A\x00B=1\nGOOD=2",
    ],
)
def test_invalid_imported_env_line_is_reported_and_does_not_hide_valid_lines(text):
    warnings = []
    result = _text_to_env(text, warnings)
    assert result == {"GOOD": "2"} and warnings


@pytest.mark.parametrize("value", ["a=b=c", "space value", "ゲーム", "/tmp/path with spaces"])
def test_valid_env_value_survives_parse(value):
    assert _text_to_env("VALUE=" + value) == {"VALUE": value}
