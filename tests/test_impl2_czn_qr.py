# SPDX-License-Identifier: GPL-3.0-or-later
"""F-06: when STOVE sends no QR image, show the login link instead of nothing."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.events import Event
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_a_missing_qr_png_shows_the_selectable_login_link(qapp):
    view = GameView()
    try:
        view.append_event(
            Event(
                "step",
                "Scan the QR code with the STOVE app",
                data={"png": None, "url": "https://stove.example/qr"},
            )
        )

        assert not view.qr_frame.isHidden()
        assert not view.qr_frame.link.isHidden()
        assert "https://stove.example/qr" in view.qr_frame.link.text()
        assert "Could not display" in view.qr_hint.text()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_a_broken_qr_path_falls_back_to_the_url(qapp, tmp_path):
    view = GameView()
    try:
        view.append_event(
            Event(
                "step",
                "Scan the QR code with the STOVE app",
                data={"png": str(tmp_path / "missing.png"), "url": "https://stove.example/qr"},
            )
        )

        assert not view.qr_frame.link.isHidden()
        assert "https://stove.example/qr" in view.qr_frame.link.text()
    finally:
        view.deleteLater()
        qapp.processEvents()
