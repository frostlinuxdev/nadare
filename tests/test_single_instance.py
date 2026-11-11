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


# ---- #624: a second start with --profile shows that game in the running window -------------------
def test_a_second_start_with_a_profile_asks_the_first_to_show_it(qtbot, name):
    primary = SingleInstance(name)
    try:
        assert primary.start() is True
        activations: list[int] = []
        profiles: list[str] = []
        primary.activation_requested.connect(lambda: activations.append(1))
        primary.profile_requested.connect(profiles.append)

        secondary = SingleInstance(name, profile="genshin")
        try:
            assert secondary.start() is False
            qtbot.waitUntil(lambda: profiles == ["genshin"], timeout=3000)
            assert activations
        finally:
            secondary.close()
    finally:
        primary.close()


@pytest.mark.parametrize(
    "payload",
    [b"show-profile:../escape\n", b"show-profile:" + b"x" * 300 + b"\n", b"show-profile:\n",
     b"show-profile:a\x00b\n", b"show-profile:ok\nextra"],
)
def test_a_bad_profile_request_does_nothing(qtbot, name, payload):
    from PyQt6.QtNetwork import QLocalSocket

    primary = SingleInstance(name)
    try:
        assert primary.start() is True
        seen: list[object] = []
        primary.activation_requested.connect(lambda: seen.append("activate"))
        primary.profile_requested.connect(seen.append)
        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        socket.write(payload)
        socket.flush()
        socket.waitForBytesWritten(1000)
        qtbot.wait(200)
        socket.disconnectFromServer()
        qtbot.wait(100)
        assert seen == []
    finally:
        primary.close()


def test_the_window_side_opens_a_known_profile_and_ignores_others():
    from nadare.gui import _show_profile

    class FakeWindow:
        def __init__(self):
            self.config = type("C", (), {"profiles": {"genshin": {}}})()
            self.opened: list[str] = []

        def open_game(self, profile):
            self.opened.append(profile)

    window = FakeWindow()
    _show_profile(window, "genshin")
    _show_profile(window, "missing")
    assert window.opened == ["genshin"]


def test_run_passes_the_profile_to_the_lock_and_wires_the_signal():
    from pathlib import Path

    text = Path("src/nadare/gui/__init__.py").read_text(encoding="utf-8")
    assert "SingleInstance(profile=profile)" in text
    assert "profile_requested.connect" in text
