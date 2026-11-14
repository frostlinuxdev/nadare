# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The sign-in QR code panel.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QPixmap,
)

from .shared import (
    _QrPanel,
)


class QrMixin:
    """Mixin for :class:`GameView`; see the module text."""

    def _show_qr(self, path: str | None) -> bool:
        """Render the STOVE login QR from ``event.data['png']`` (host path)."""
        if not path:
            return False
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return False
        size = _QrPanel.QR_SIZE
        if pixmap.width() != size:
            # Nearest-neighbour keeps the modules crisp, which scanners prefer.
            mode = (
                Qt.TransformationMode.FastTransformation
                if pixmap.width() < size
                else Qt.TransformationMode.SmoothTransformation
            )
            pixmap = pixmap.scaledToWidth(size, mode)
        self.qr_frame.link.hide()
        self.qr_label.show()
        self.qr_label.setPixmap(pixmap)
        self.qr_hint.setText(self._QR_HINTS["scan"])
        self.qr_frame.show()
        self.qr_frame.raise_()
        self.qr_frame.reposition()
        return True

    def _show_qr_link(self, url: str) -> None:
        """Show the login link when the QR image is missing or unreadable."""
        self.qr_label.hide()
        self.qr_hint.setText(
            "Could not display the QR code. Open this link in the STOVE app on your phone:"
        )
        self.qr_frame.link.setText(url)
        self.qr_frame.link.show()
        self.qr_frame.show()
        self.qr_frame.raise_()
        self.qr_frame.reposition()

    def show_qr(self, path: str | None) -> None:
        """Re-show a still-live login QR (e.g. when returning to a busy login)."""
        self._show_qr(path)

    def _set_qr_state(self, state: str) -> None:
        """Reflect the coarse QR login state under the code."""
        hint = self._QR_HINTS.get(state)
        if hint:
            self.qr_hint.setText(hint)
        if state == "expired":
            self.qr_label.clear()

    def _hide_qr(self) -> None:
        self.qr_frame.hide()
        self.qr_label.clear()
        self.qr_frame.link.clear()
        self.qr_frame.link.hide()
