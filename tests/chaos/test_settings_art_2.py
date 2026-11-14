# SPDX-License-Identifier: GPL-3.0-or-later
"""A custom background copied into Nadare's folder must survive the next Settings save."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
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


@pytest.fixture
def rig(tmp_path, qapp, monkeypatch):
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
    page = SettingsPage()
    yield page, tmp_path
    page.deleteLater()
    qapp.processEvents()


def test_the_managed_copy_survives_a_second_save_after_the_source_is_gone(rig, qapp):
    page, tmp_path = rig
    source = tmp_path / "my-art.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nchaos")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="wine", title="Sample")
    config.save()

    page.load(config, "sample")
    _settle(qapp)
    page.background.setText(str(source))
    page.save()
    _settle(qapp)

    saved = Config.load(config.path).profiles["sample"]["custom_background"]
    print("first save: field =", page.background.text())
    print("first save: config =", saved)
    assert saved != str(source) and Path(saved).is_file(), "the image was not copied"

    source.unlink()  # the user tidies up the picture they picked
    page.title.setText("Sample renamed")
    page.save()
    _settle(qapp)

    again = Config.load(config.path).profiles["sample"]["custom_background"]
    print("second save: config =", again, "exists =", Path(again).exists())
    assert again == saved, "the second save replaced the managed copy with the deleted source"
    assert Path(again).is_file(), "the background now points at a file that does not exist"
