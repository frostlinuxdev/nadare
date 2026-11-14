# SPDX-License-Identifier: GPL-3.0-or-later
"""An unrecognised saved choice must not be silently rewritten by an unrelated save."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _parts_patch import patch_everywhere

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(4):
        qapp.processEvents()


def test_an_unknown_renderer_is_not_replaced_by_auto_on_save(tmp_path, qapp, monkeypatch):
    patch_everywhere(
        monkeypatch,
        settings_mod,
        "create_backend",
        lambda profile, config: SimpleNamespace(
            status=lambda: SimpleNamespace(state="not_installed", data={})
        ),
    )
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    config = Config.load(tmp_path / "config.json")
    # A value from a newer build (or a hand edit) that this build does not list.
    config.add_profile("sample", backend="wine", title="Sample", renderer="vulkan_native")
    config.save()
    page = SettingsPage()
    try:
        page.load(config, "sample")
        _settle(qapp)
        shown = page.option_widget("renderer").currentData()
        print("saved choice = vulkan_native, shown =", repr(shown))

        page.title.setText("Sample renamed")  # a change unrelated to the renderer
        page.save()
        _settle(qapp)

        after = Config.load(config.path).profiles["sample"].get("renderer")
        print("after saving an unrelated change =", repr(after))
        assert after == "vulkan_native", "Settings silently replaced an unknown saved choice"
    finally:
        page.deleteLater()
        qapp.processEvents()
