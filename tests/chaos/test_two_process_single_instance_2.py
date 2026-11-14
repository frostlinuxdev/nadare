# SPDX-License-Identifier: GPL-3.0-or-later
"""A regular file at the socket name is not a stale socket and must not be deleted."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only without PyQt6
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from pathlib import Path

from nadare.gui.single_instance import SingleInstance


@pytest.fixture
def name(tmp_path) -> str:
    # A short absolute socket path, like the real server_name().
    return str(tmp_path / "s.sock")


def test_a_regular_file_at_the_socket_path_is_left_alone(name):
    path = Path(name)
    path.write_text("settings the user keeps here", encoding="utf-8")

    instance = SingleInstance(name)
    try:
        started = instance.start()
        status = instance.status
        exists = path.exists()
        content = path.read_text(encoding="utf-8") if exists and path.is_file() else ""
        print("start():", started)
        print("status:", status)
        print("path still a regular file:", exists and path.is_file())
        print("content after start:", content)
        assert exists and path.is_file(), "the single-instance lock deleted a regular file"
        assert content == "settings the user keeps here"
    finally:
        instance.close()
