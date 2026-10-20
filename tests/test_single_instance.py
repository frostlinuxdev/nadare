# SPDX-License-Identifier: GPL-3.0-or-later
"""Single-instance guard: a second start activates the first and bows out."""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.single_instance import SingleInstance


@pytest.fixture
def name() -> str:
    return f"nadare-test-{os.getpid()}-{uuid.uuid4().hex}"


def test_second_attempt_activates_first(qtbot, name):
    primary = SingleInstance(name)
    try:
        assert primary.start() is True
        activations: list[int] = []
        primary.activation_requested.connect(lambda: activations.append(1))

        secondary = SingleInstance(name)
        try:
            assert secondary.start() is False
            qtbot.waitUntil(lambda: bool(activations), timeout=3000)
        finally:
            secondary.close()
    finally:
        primary.close()


def test_stale_socket_is_reclaimed(qtbot, name):
    first = SingleInstance(name)
    assert first.start() is True
    first.close()

    second = SingleInstance(name)
    try:
        assert second.start() is True
    finally:
        second.close()
