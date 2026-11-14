# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Helpers and constants shared by the parts of nadare.gui.game_view.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import (
    QPixmap,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..images import ImageLoader
from ..theme import ACCENT, cover_pixmap

#: News thumbnails stay 16:9 but never grow taller than this (px).
_NEWS_THUMB_MAX_H = 240

#: Under the login QR: what to do, and the one safety rule for QR logins.
_QR_SCAN_HINT = (
    "Scan this QR code with the STOVE mobile app (new to STOVE? get it at "
    "https://www.onstove.com). Only scan a code shown here, in your own Nadare window."
)


def _make_keyboard_link(widget: QWidget, selector: str, name: str) -> None:
    """Let Tab reach a clickable card, show where it is, and give it a name (#901).

    ``selector`` is the Qt type and object name the focus ring is styled on."""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setAccessibleName(name)
    widget.setStyleSheet(f"{selector}:focus {{ border: 2px solid {ACCENT}; }}")


def _format_news_date(published_at: Any) -> str:
    """Format an epoch-millisecond timestamp as ``YYYY-MM-DD`` (best effort)."""
    try:
        millis = int(published_at or 0)
    except (TypeError, ValueError):
        return ""
    if millis <= 0:
        return ""
    try:
        return datetime.fromtimestamp(millis / 1000).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


class ElidedLabel(QLabel):
    """A word-wrapped label limited to ``max_lines`` with an elided last line."""

    def __init__(self, text: str = "", max_lines: int = 2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_lines = max(1, int(max_lines))
        self._full_text = ""
        self.setWordWrap(True)
        # Remote titles are data, never markup.
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = str(text or "")
        self.setToolTip(Qt.convertFromPlainText(self._full_text))
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        width = self.width()
        text = self._full_text
        if width <= 1 or not text:
            super().setText(text)
            return
        metrics = self.fontMetrics()
        lines: list[str] = []
        current = ""
        truncated = False
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if not current or metrics.horizontalAdvance(candidate) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) == self._max_lines:
                    truncated = True
                    break
        if not truncated:
            if current:
                lines.append(current)
            if len(lines) > self._max_lines:
                lines = lines[: self._max_lines]
                truncated = True
        if truncated and lines:
            lines[-1] = metrics.elidedText(lines[-1] + " …", Qt.TextElideMode.ElideRight, width)
        display = "\n".join(lines) if lines else text
        if display != self.text():
            super().setText(display)


class _ThumbnailLabel(QLabel):
    """A 16:9 cover-cropped news thumbnail that re-crops as the card resizes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NewsThumb")
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._source: QPixmap | None = None
        self._seed = "news"
        self._last_size = (0, 0)

    def set_source(self, source: QPixmap | None, seed: str) -> None:
        self._source = source if (source is not None and not source.isNull()) else None
        self._seed = seed
        self._last_size = (0, 0)
        self._refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        width = self.width()
        if width <= 1:
            return
        # 16:9, capped so a maximised window still shows a full row of cards.
        height = min(_NEWS_THUMB_MAX_H, round(width * 9 / 16))
        height = max(height, 1)
        if height != self.height():
            self.setFixedHeight(height)
        if (width, height) == self._last_size:
            return
        self._last_size = (width, height)
        self.setPixmap(cover_pixmap(self._source, width, height, self._seed, radius=10))


class NewsCard(QFrame):
    """One clickable official news item (thumbnail, title, meta)."""

    clicked = pyqtSignal(str)  # article url

    def __init__(
        self,
        item: dict[str, Any],
        parent: QWidget | None = None,
        loader: ImageLoader | None = None,
        fallback: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("NewsCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._url = str(item.get("url") or "")
        self._title = str(item.get("title") or "")
        self.setToolTip(self._url)
        if self._url:
            _make_keyboard_link(
                self, "QFrame#NewsCard", self._title or "Open the article in your browser"
            )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.thumb = _ThumbnailLabel(self)
        image_path = item.get("image_path")
        seed = str(item.get("title") or "news")
        # Items without their own image (update notices) show the game's art
        # rather than a bare gradient that reads as "image failed to load".
        candidates = [str(image_path)] if image_path else []
        candidates += [str(path) for path in fallback or [] if path]
        if loader is not None:
            loader.request(candidates, 480, 270, self._apply_thumb, crop=True)
        else:
            path = ImageLoader.resolve(candidates)
            self.thumb.set_source(QPixmap(path) if path else None, seed)
        layout.addWidget(self.thumb)

        self.title_label = ElidedLabel(str(item.get("title") or ""), max_lines=2)
        self.title_label.setObjectName("NewsTitle")
        layout.addWidget(self.title_label)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        category = str(item.get("category") or "").strip()
        if category:
            self.category_label = QLabel(category.upper())
            self.category_label.setTextFormat(Qt.TextFormat.PlainText)
            self.category_label.setObjectName("NewsCategory")
            meta.addWidget(self.category_label)
        self.meta_label = QLabel(_format_news_date(item.get("published_at")))
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setObjectName("Dim")
        meta.addWidget(self.meta_label)
        meta.addStretch(1)
        layout.addLayout(meta)

    def _apply_thumb(self, pixmap: QPixmap | None) -> None:
        """Apply a decoded thumbnail; ignore a card deleted by a fast switch."""
        try:
            self.thumb.set_source(pixmap, self._title or "news")
        except RuntimeError:
            return  # the underlying C++ widget is gone

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            if self._url:
                self.clicked.emit(self._url)
        super().mouseReleaseEvent(event)


    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._url and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit(self._url)
            event.accept()
            return
        super().keyPressEvent(event)

_REMEMBER_TEXT = "Remember me on this device"

_REMEMBER_TIP = (
    "Keeps you logged in on this device, so you do not need to scan the QR code "
    "each time. Turning it off deletes the saved login."
)

_SANDBOX_TIP = (
    "Hides your home folder from the game, so it cannot read your files. The game can "
    "still use the network. Needs bubblewrap, which comes with the game runtime."
)


class _QrPanel(QFrame):
    """The STOVE login QR as a card overlaid on the launcher (not a new window).

    It used to sit inline at the bottom of the game page, under the progress bar
    and often off screen. As an overlay it is always visible and centred, and it
    needs no extra window (which Wayland would place wherever it likes).
    """

    QR_SIZE = 320
    cancel_clicked = pyqtSignal()
    remember_clicked = pyqtSignal(bool)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("StatsBar")
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(12)
        title = QLabel("Scan to log in")
        title.setObjectName("HeroKicker")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(self.QR_SIZE + 8, self.QR_SIZE + 8)
        layout.addWidget(self.label)
        self.hint = QLabel(_QR_SCAN_HINT)
        self.hint.setObjectName("Hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        # Fallback when STOVE sends a login link but no QR image: the URL is
        # selectable so it can still be opened in the phone app.
        self.link = QLabel()
        self.link.setObjectName("Hint")
        self.link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.link.setWordWrap(True)
        self.link.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.link.hide()
        layout.addWidget(self.link)
        # Where the decision is made: tick it before approving the scan.
        self.remember = QCheckBox(_REMEMBER_TEXT)
        self.remember.setToolTip(_REMEMBER_TIP)
        self.remember.setVisible(False)
        self.remember.clicked.connect(self.remember_clicked.emit)
        layout.addWidget(self.remember, 0, Qt.AlignmentFlag.AlignCenter)
        self.cancel_button = QPushButton("Cancel login")
        self.cancel_button.setObjectName("GhostButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.cancel_clicked.emit)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.hide()

    def reposition(self) -> None:
        """Centre over the parent page, kept inside its bounds."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(0, (parent.width() - self.width()) // 2)
        y = max(0, (parent.height() - self.height()) // 2)
        self.move(x, y)
