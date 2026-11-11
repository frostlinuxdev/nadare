# SPDX-License-Identifier: GPL-3.0-or-later
"""#971 item 3: Move holds the operation lock until the new paths are saved.

The worker released the lock when the files had moved, while the profile still named the old
path until a queued GUI callback saved the new one. In that gap another window or the CLI could
take the lock and work on a folder that no longer existed.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from test_settings_lock_async import _move_setup, _settle, qapp  # noqa: F401 - fixture reuse

from nadare.core import process, relink, relocate
from nadare.gui.settings import SettingsPage


def test_the_lock_is_held_while_the_new_paths_are_saved(tmp_path, qapp, monkeypatch):  # noqa: F811
    held: list[bool] = []
    real_commit = SettingsPage._commit_relink

    def commit(self, config, name, plan):
        held.append(process.operation_in_progress(name))
        return real_commit(self, config, name, plan)

    monkeypatch.setattr(SettingsPage, "_commit_relink", commit)
    page, config, src, parent, _texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert held == [True]
        assert config.profiles["sample"]["prefix"] == str(parent / "pfx")
        assert not process.operation_in_progress("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_failed_move_frees_the_lock(tmp_path, qapp, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise relocate.RelocateError("disk full")

    monkeypatch.setattr(relocate, "move_tree", broken)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert any("disk full" in t for t in texts)
        assert not process.operation_in_progress("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_failed_relink_after_the_move_frees_the_lock(tmp_path, qapp, monkeypatch):  # noqa: F811
    def refuse(*args, **kwargs):
        raise relink.RelinkError("cannot relink")

    monkeypatch.setattr(relink, "plan_relink", refuse)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert any("cannot relink" in t for t in texts)
        assert not process.operation_in_progress("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_failed_save_frees_the_lock(tmp_path, qapp, monkeypatch):  # noqa: F811
    monkeypatch.setattr(SettingsPage, "_commit_relink", lambda self, config, name, plan: False)
    page, config, src, parent, texts = _move_setup(tmp_path, qapp, monkeypatch)
    try:
        page.move_button.click()
        _settle(qapp)
        assert not process.operation_in_progress("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()
