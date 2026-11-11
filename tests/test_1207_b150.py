# SPDX-License-Identifier: GPL-3.0-or-later
"""The install-preview must not walk the library on the GUI thread."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import add_game


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_typing_a_library_path_does_not_scan_it_on_the_gui_thread(tmp_path, qapp, monkeypatch):
    main_thread_calls = []

    def fake_reusable_folder(*_args, **_kwargs):
        if threading.current_thread() is threading.main_thread():
            main_thread_calls.append(True)
        return None

    monkeypatch.setattr(add_game.libraries, "reusable_folder", fake_reusable_folder)
    dialog = add_game.AddGameDialog(
        Config.load(tmp_path / "config.json"),
        data_root=tmp_path / "data",
        kind="direct-bd2",
    )
    try:
        dialog.location_toggle.click()
        dialog.set_install_dir(str(tmp_path / "Games"))
        main_thread_calls.clear()
        dialog.folder.setText(str(tmp_path / "Games" / "x"))
        assert not main_thread_calls, "the folder preview walked the library on the GUI thread"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()
