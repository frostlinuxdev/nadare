# SPDX-License-Identifier: GPL-3.0-or-later
"""A garbage client must not grow the single-instance buffer without limit."""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only without PyQt6
    from PyQt6.QtNetwork import QLocalSocket
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.single_instance import SingleInstance


@pytest.fixture
def name() -> str:
    return f"nadare-test-{os.getpid()}-{uuid.uuid4().hex}"


def test_a_huge_whitespace_message_cannot_grow_the_buffer(qtbot, name):
    primary = SingleInstance(name)
    try:
        assert primary.start() is True
        activations: list[int] = []
        primary.activation_requested.connect(lambda: activations.append(1))

        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)

        # Whitespace strips to b"" -- a prefix of "activate" -- so the server keeps waiting
        # for the rest forever and every read appends to the same in-memory buffer.
        chunk = b" " * 4096
        for _ in range(128):  # 512 KiB, far beyond the 271-byte profile limit
            socket.write(chunk)
            socket.flush()
            socket.waitForBytesWritten(1000)
            QApplication.processEvents()
            qtbot.wait(2)

        held = max(
            (len(buffer) for buffer in primary._buffers.values()),
            default=0,
        )
        print("bytes held for one garbage connection:", held)
        print("activation requests:", activations)
        assert held <= 4096, "a garbage message grew the server buffer without a limit"
        assert activations == []
        socket.disconnectFromServer()
    finally:
        primary.close()
