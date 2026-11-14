# SPDX-License-Identifier: GPL-3.0-or-later
"""A NUL byte in a game folder/prefix must not crash Settings (load or save)."""

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
    shown: list[str] = []
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: shown.append(a[2]))
    page = SettingsPage()
    yield page, shown, tmp_path
    page.deleteLater()
    qapp.processEvents()


def test_loading_a_game_folder_with_a_nul_does_not_raise(rig, qapp):
    page, shown, tmp_path = rig
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "bd2",
        backend="direct-bd2",
        title="Brown Dust 2",
        install_dir="bad\x00folder",
        prefix=str(tmp_path / "prefix"),
    )
    config.save()

    raised = None
    try:
        page.load(config, "bd2")
        _settle(qapp)
    except Exception as exc:  # noqa: BLE001 - this is the bug under test
        raised = exc
    print("load raised:", repr(raised))
    assert raised is None, "a NUL in a saved game folder crashed Settings while opening"


def test_saving_a_folder_with_a_nul_is_refused_without_a_traceback(rig, qapp):
    page, shown, tmp_path = rig
    config = Config.load(tmp_path / "config.json")
    config.add_profile("winegame", backend="wine", title="Mine")
    config.save()
    page.load(config, "winegame")
    _settle(qapp)

    page.option_widget("prefix").setText("bad\x00prefix")
    raised = None
    try:
        page.save()
        _settle(qapp)
    except Exception as exc:  # noqa: BLE001 - this is the bug under test
        raised = exc
    print("save raised:", repr(raised), "messages:", shown)
    assert raised is None, "a NUL in the prefix field crashed Settings instead of being refused"
    assert shown, "the user was not told why the folder was refused"
