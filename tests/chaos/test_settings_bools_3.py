# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand-edited "false" for a per-game boolean option must mean off, not on."""

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
    page = SettingsPage()
    yield page, tmp_path
    page.deleteLater()
    qapp.processEvents()


CASES = [
    ("launcher-nikke", "launcherless"),
    ("launcher-nikke", "stop_when_hidden"),
    ("wine", "vcredist"),
    ("steam", "stop_steam"),
]


@pytest.mark.parametrize(("kind", "key"), CASES)
def test_a_saved_text_false_is_shown_as_off(rig, qapp, kind, key):
    page, tmp_path = rig
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend=kind, title="Sample", **{key: "false"})
    config.save()

    page.load(config, "sample")
    _settle(qapp)
    box = page.option_widget(key)
    print(f"{kind}.{key}: config={config.profile('sample')[key]!r} checked={box.isChecked()}")
    assert not box.isChecked(), f"{kind}.{key}: 'false' in the config opened as checked"


@pytest.mark.parametrize(("kind", "key"), CASES)
def test_saving_does_not_turn_a_saved_text_false_into_true(rig, qapp, kind, key):
    page, tmp_path = rig
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend=kind, title="Sample", **{key: "false"})
    config.save()

    page.load(config, "sample")
    _settle(qapp)
    page.save()
    _settle(qapp)

    after = Config.load(config.path).profiles["sample"].get(key)
    print(f"{kind}.{key}: after save={after!r}")
    assert after is False or after is None, f"{kind}.{key}: saving turned 'false' into {after!r}"
