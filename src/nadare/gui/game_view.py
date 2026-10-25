# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-game page: hero header, status, the one primary action, progress and log.

This widget renders state and forwards user intent as signals; it never calls a
backend itself. The main window owns the :class:`BackendController` and wires
those signals to workers.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import paths, userns
from ..core.backends import Status, page_notice, supports_news, supports_update
from ..core.events import Event
from ..core.news import CREDIT
from .images import ImageLoader
from .library import backend_label, profile_art_candidates, profile_title
from .theme import badge_style, cover_pixmap, set_state_property
from .userns_dialog import UsernsDialog


@dataclass
class GameSnapshot:
    """Last rendered state for one profile, replayed instantly on a switch."""

    status: Status | None = None
    news: Any = None
    media: dict | None = None
    qr: str | None = None


#: Primary button label for each operation.
PRIMARY_LABELS = {
    "install": "INSTALL",
    "update": "UPDATE",
    "login": "LOG IN",
    "launch": "LAUNCH",
    "stop": "STOP",
    "settings": "SET GAME EXE IN SETTINGS",
    "retry": "RETRY",
}

#: User-facing operation names used in dialogs (never the raw operation id).
OPERATION_TITLES = {
    "install": "Installing",
    "update": "Updating",
    "repair": "Repairing",
    "remake_prefix": "Rebuilding the Wine prefix",
    "launch": "Launching",
    "verify": "Checking files",
    "login": "Signing in",
    "status": "Checking",
    "remove": "Removing",
}


def operation_title(operation: str) -> str:
    """Friendly title for ``operation``; unknown ids degrade readably."""
    return OPERATION_TITLES.get(operation, operation.capitalize())

#: Busy operation -> pill state shown while it runs.
_BUSY_STATES = {
    "install": "installing",
    "update": "updating",
    "repair": "repairing",
    "remake_prefix": "rebuilding",
    "launch": "launching",
    "login": "login",
    "verify": "verifying",
    "stop": "stopping",
    "uninstall": "uninstalling",
    "set_remember": "saving",
    "logout": "logging out",
}

#: Log line colours by event kind.
_LOG_COLORS = {
    "error": "#ff8a8a",
    "done": "#6ea8ff",
    "status": "#85868d",
    "step": "#8ab4ff",
    "progress": "#85868d",
    "log": "#c7cad0",
}

#: News thumbnails stay 16:9 but never grow taller than this (px).
_NEWS_THUMB_MAX_H = 240

#: Operation banner height (px).
_BANNER_H = 110

#: A second click right after a button turns into CANCEL must not cancel the new job.
_CANCEL_GRACE_S = 0.5


#: Under the login QR: what to do, and the one safety rule for QR logins.
_QR_SCAN_HINT = (
    "Scan this QR code with the STOVE mobile app. "
    "Only scan a code shown here, in your own Nadare window."
)


class HeroCard(QFrame):
    """The game hero panel: an optional full-bleed image under a dark gradient.

    The gradient runs left-to-right so the title and primary button (which sit
    in the left column) stay readable over busy official key art.  Without an
    image it paints exactly like the plain ``Hero`` card.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Hero")
        self._source: QPixmap | None = None

    def set_source(self, source: QPixmap | None) -> None:
        self._source = source if (source is not None and not source.isNull()) else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if self._source is None:
            return
        width, height = self.width(), self.height()
        if width <= 1 or height <= 1:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, width, height), 16, 16)
        painter.setClipPath(path)
        scaled = self._source.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        painter.drawPixmap(0, 0, scaled.copy(x, y, width, height))
        gradient = QLinearGradient(0, 0, width * 0.72, 0)
        gradient.setColorAt(0.0, QColor(7, 9, 13, 245))
        gradient.setColorAt(0.5, QColor(7, 9, 13, 185))
        gradient.setColorAt(1.0, QColor(7, 9, 13, 0))
        painter.fillRect(0, 0, width, height, gradient)
        painter.end()


class _BannerCard(QLabel):
    """One clickable operation banner image, decoded off the GUI thread."""

    clicked = pyqtSignal(str)

    def __init__(
        self,
        image_path: str,
        url: str,
        parent: QWidget | None = None,
        loader: ImageLoader | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self._url = url
        if url:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(url)
        # A fixed placeholder keeps the strip from jumping while the decode runs.
        self.setFixedSize(round(_BANNER_H * 16 / 9), _BANNER_H)
        if loader is not None:
            loader.request(
                [str(image_path)], 2000, _BANNER_H, self._apply, crop=False
            )
        else:
            self._apply(QPixmap(str(image_path)))

    def _apply(self, pixmap: QPixmap | None) -> None:
        try:
            if pixmap is None or pixmap.isNull():
                return
            width = max(1, round(pixmap.width() * _BANNER_H / max(1, pixmap.height())))
            self.setPixmap(
                pixmap.scaled(
                    width,
                    _BANNER_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.setFixedSize(width, _BANNER_H)
        except RuntimeError:
            return  # the card was removed by a fast profile switch

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if (
            self._url
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit(self._url)
        super().mouseReleaseEvent(event)


class BannerStrip(QWidget):
    """A slim horizontally-scrolling strip of official operation banners."""

    clicked = pyqtSignal(str)

    def __init__(
        self, parent: QWidget | None = None, loader: ImageLoader | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("BannerStrip")
        self._loader = loader
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("BannerScroll")
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(_BANNER_H + 14)
        container = QWidget()
        self._row = QHBoxLayout(container)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(10)
        self._scroll.setWidget(container)
        self._container = container
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)
        self.setVisible(False)

    def set_banners(self, banners: list[dict]) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for banner in banners or []:
            path = banner.get("image_path") if isinstance(banner, dict) else None
            if not path:
                continue
            card = _BannerCard(
                str(path), str(banner.get("url") or ""), self._container, self._loader
            )
            card.clicked.connect(self.clicked.emit)
            self._row.addWidget(card)
        self._container.adjustSize()
        self.setVisible(self._row.count() > 0)


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
            lines[-1] = metrics.elidedText(
                lines[-1] + " …", Qt.TextElideMode.ElideRight, width
            )
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


#: Backwards-compatible alias for the old single-row widget name.
NewsRow = NewsCard


def _version_text(version: object) -> str:
    """``v``-prefixed version for the pill row; some backends already include the ``v``."""
    text = str(version or "").strip()
    if not text:
        return ""
    return text if text[0] in "vV" and text[1:2].isdigit() else f"v{text}"


_REMEMBER_TEXT = "Remember me on this device"
_REMEMBER_TIP = (
    "Keeps you logged in by storing the login in your system keyring, so you "
    "do not need to scan the QR code each time. Turning it off deletes the saved login."
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


class GameView(QWidget):
    """The single-game launch page."""

    action_requested = pyqtSignal(str)  # operation name
    cancel_requested = pyqtSignal()
    back_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    remember_toggled = pyqtSignal(bool)  # the user ticked/unticked Remember me

    #: Text shown under the login QR for each coarse auth state.
    _QR_HINTS = {
        "scan": _QR_SCAN_HINT,
        "waiting": "Waiting for you to approve the scan in the STOVE app…",
        "approved": "Approved — finishing login…",
        "expired": "QR code expired. Press Log in again to get a new one.",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.images = ImageLoader(parent=self)
        self._profile = ""
        self._profile_data: dict[str, Any] = {}
        self._status: Status | None = None
        self._busy_op: str | None = None
        self._primary_grace_until = 0.0
        self._news_shown = False  # intended news visibility (hidden while busy)
        self._started_at = 0.0
        self._console_expanded = True
        self.news_columns = 1
        self._profile_seq = 0
        self._media_seq = 0

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("GameScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The page height depends on its width (16:9 thumbnails, wrapped titles),
        # so an as-needed bar can flip on and off with each resize it causes and
        # recurse until the stack overflows. Always reserve the (transparent) track.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        outer.addWidget(self.scroll)

        content = QWidget()
        content.setObjectName("Page")
        self.scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 24, 32, 28)
        root.setSpacing(16)

        # ---- breadcrumb header --------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(12)
        self.back = QPushButton("←  Library")
        self.back.setObjectName("GhostButton")
        self.back.clicked.connect(self.back_requested.emit)
        header.addWidget(self.back)
        header.addStretch(1)
        self.settings = QPushButton("Settings")
        self.settings.setObjectName("GhostButton")
        self.settings.clicked.connect(self.settings_requested.emit)
        header.addWidget(self.settings)
        root.addLayout(header)

        # ---- hero ----------------------------------------------------------
        hero = HeroCard()
        self.hero = hero
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 24, 26, 24)
        hero_layout.setSpacing(26)

        copy = QVBoxLayout()
        copy.setSpacing(9)
        self.kicker = QLabel("NOT PLAYED YET")
        self.kicker.setObjectName("HeroKicker")
        copy.addWidget(self.kicker)

        self.title = QLabel("")
        self.title.setObjectName("HeroTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        copy.addWidget(self.title)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(9)
        self.badge = QLabel("")
        self.badge.setObjectName("Pill")
        meta_row.addWidget(self.badge)
        self.backend_label = QLabel("")
        self.backend_label.setObjectName("Muted")
        meta_row.addWidget(self.backend_label)
        self.version_label = QLabel("")
        self.version_label.setObjectName("Dim")
        meta_row.addWidget(self.version_label)
        self.account_label = QLabel("")
        self.account_label.setObjectName("Muted")
        self.account_label.setVisible(False)
        meta_row.addWidget(self.account_label)
        meta_row.addStretch(1)
        copy.addLayout(meta_row)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("Hint")
        self.detail_label.setWordWrap(True)
        copy.addWidget(self.detail_label)

        # Informational note from the backend (e.g. "coming to Steam").
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("Hint")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextFormat(Qt.TextFormat.RichText)
        self.notice_label.setOpenExternalLinks(True)
        self.notice_label.setVisible(False)
        copy.addWidget(self.notice_label)
        copy.addStretch(1)

        self.primary = QPushButton("LAUNCH")
        self.primary.setObjectName("Primary")
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.primary.setMinimumHeight(52)
        self.primary.clicked.connect(self._on_primary)
        copy.addWidget(self.primary)

        secondary = QHBoxLayout()
        secondary.setSpacing(8)
        self.login = QPushButton("Log in")
        self.login.setObjectName("GhostButton")
        self.login.clicked.connect(lambda: self.action_requested.emit("login"))
        self.login.setVisible(False)
        self.logout = QPushButton("Log out")
        self.logout.setObjectName("GhostButton")
        self.logout.clicked.connect(lambda: self.action_requested.emit("logout"))
        self.logout.setVisible(False)
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("GhostButton")
        self.update_btn.clicked.connect(lambda: self.action_requested.emit("update"))
        self.verify = QPushButton("Check game files")
        self.verify.setObjectName("GhostButton")
        self.verify.setToolTip(
            "Compares the installed game files with the official list. "
            "Does not change or download anything."
        )
        self.verify.clicked.connect(lambda: self.action_requested.emit("verify"))
        self.repair_btn = QPushButton("Repair files")
        self.repair_btn.setObjectName("GhostButton")
        self.repair_btn.setToolTip(
            "Re-check every game file and re-download the ones that are missing or damaged"
        )
        self.repair_btn.clicked.connect(lambda: self.action_requested.emit("repair"))
        self.logs = QPushButton("Open logs")
        self.logs.setObjectName("GhostButton")
        self.logs.clicked.connect(self._open_logs)
        for button in (
            self.login,
            self.logout,
            self.update_btn,
            self.verify,
            self.repair_btn,
            self.logs,
        ):
            secondary.addWidget(button)
        secondary.addStretch(1)
        copy.addLayout(secondary)

        self.remember = QCheckBox(_REMEMBER_TEXT)
        self.remember.setToolTip(_REMEMBER_TIP)
        self.remember.setVisible(False)
        self.remember.clicked.connect(self._on_remember_clicked)
        copy.addWidget(self.remember)

        hero_layout.addLayout(copy, 1)

        self.art = QFrame()
        self.art.setObjectName("ArtFrame")
        self.art.setFixedSize(212, 268)
        art_layout = QVBoxLayout(self.art)
        art_layout.setContentsMargins(6, 6, 6, 6)
        self.art_label = QLabel()
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        art_layout.addWidget(self.art_label)
        hero_layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)

        root.addWidget(hero)

        # ---- operation banners (official media) ---------------------------
        self.banners = BannerStrip(self, loader=self.images)
        self.banners.clicked.connect(self._open_external)
        root.addWidget(self.banners)

        # ---- news (publisher's own feed, when the backend has one) --------
        self.news_frame = QFrame()
        self.news_frame.setObjectName("NewsSection")
        news_layout = QVBoxLayout(self.news_frame)
        news_layout.setContentsMargins(16, 12, 16, 14)
        news_layout.setSpacing(6)
        news_head = QHBoxLayout()
        news_label = QLabel("NEWS")
        news_label.setObjectName("Section")
        news_head.addWidget(news_label)
        news_head.addStretch(1)
        self.news_hint = QLabel("News unavailable")
        self.news_hint.setObjectName("Muted")
        news_head.addWidget(self.news_hint, 0, Qt.AlignmentFlag.AlignRight)
        self.news_retry = QPushButton("Retry")
        self.news_retry.setObjectName("GhostButton")
        self.news_retry.setVisible(False)
        self.news_retry.clicked.connect(lambda: self.action_requested.emit("news"))
        news_head.addWidget(self.news_retry, 0, Qt.AlignmentFlag.AlignRight)
        news_layout.addLayout(news_head)
        self.news_reason = QLabel("")
        self.news_reason.setObjectName("Muted")
        self.news_reason.setWordWrap(True)
        self.news_reason.setVisible(False)
        news_layout.addWidget(self.news_reason)
        self.news_grid = QGridLayout()
        self.news_grid.setContentsMargins(0, 0, 0, 0)
        self.news_grid.setHorizontalSpacing(10)
        self.news_grid.setVerticalSpacing(10)
        news_layout.addLayout(self.news_grid)
        self.news_credit = QLabel(CREDIT)
        self.news_credit.setObjectName("Dim")
        self.news_credit.setWordWrap(True)
        news_layout.addWidget(self.news_credit)
        self.news_rows: list[NewsCard] = []
        self._news_key: tuple | None = None
        self._news_fallback: list[str] = []
        self._set_news_visible(False)
        root.addWidget(self.news_frame, 1)

        # ---- progress / step ----------------------------------------------
        progress_frame = QFrame()
        progress_frame.setObjectName("StatsBar")
        progress_layout = QVBoxLayout(progress_frame)
        progress_layout.setContentsMargins(16, 12, 16, 14)
        progress_layout.setSpacing(8)
        step_row = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setObjectName("Hint")
        self.step_label.setWordWrap(True)
        step_row.addWidget(self.step_label, 1)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("Muted")
        step_row.addWidget(self.elapsed_label, 0, Qt.AlignmentFlag.AlignRight)
        progress_layout.addLayout(step_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        progress_layout.addWidget(self.progress)
        root.addWidget(progress_frame)

        # ---- QR (STOVE login only) -----------------------------------------
        # The auth worker emits a step event whose data carries the QR png path;
        # it is shown as an in-app overlay card, centred over the page.
        self.qr_frame = _QrPanel(self)
        self.qr_label = self.qr_frame.label
        self.qr_hint = self.qr_frame.hint
        self.qr_frame.cancel_clicked.connect(self.cancel_requested.emit)
        self.qr_frame.remember_clicked.connect(self._on_remember_clicked)

        # ---- console --------------------------------------------------------
        log_head = QHBoxLayout()
        log_label = QLabel("OUTPUT")
        log_label.setObjectName("LogHeader")
        log_head.addWidget(log_label)
        log_head.addStretch(1)
        self.console_toggle = QPushButton("Show output")
        self.console_toggle.setObjectName("GhostButton")
        self.console_toggle.clicked.connect(self._toggle_console)
        log_head.addWidget(self.console_toggle)
        clear = QPushButton("Clear")
        clear.setObjectName("GhostButton")
        clear.clicked.connect(lambda: self.console.clear())
        log_head.addWidget(clear)
        root.addLayout(log_head)

        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setPlaceholderText("backend output will appear here…")
        root.addWidget(self.console, 1)

        self._set_console_expanded(True)
        self._refresh_actions()

    def _set_page_notice(self, kind: str) -> None:
        notice = page_notice(kind)
        if not notice:
            self.notice_label.clear()
            self.notice_label.setVisible(False)
            return
        text, url = notice[0], notice[1]
        label = notice[2] if len(notice) > 2 else "Learn more"
        self.notice_label.setText(
            f"{html.escape(text)} "
            f"<a href=\"{html.escape(url, quote=True)}\">{html.escape(label)}</a>"
        )
        self.notice_label.setVisible(True)

    # ---- profile -----------------------------------------------------------
    def set_profile(
        self,
        name: str,
        profile: dict[str, Any],
        snapshot: GameSnapshot | None = None,
    ) -> None:
        """Point the page at ``name``, replaying ``snapshot`` without a flash.

        The snapshot (last status/news/media) is applied before the first
        ``_refresh_actions`` so a game switch never shows an "unknown" pill or
        a CHECKING/CANCEL primary button while the background refresh runs.
        """
        self._profile = name
        self._profile_data = dict(profile)
        self._profile_seq += 1
        self._media_seq += 1
        self._status = None
        # Switching games ends the previous game's operation on this page. The
        # timer/elapsed clock belonged to that page state; MainWindow restores
        # set_busy below when the *new* profile is itself running an operation.
        self._timer.stop()
        self._started_at = 0.0
        self._busy_op = None
        self._primary_grace_until = 0.0
        title = profile_title(name, profile)
        self.title.setText(title)
        self.kicker.setText("NOT PLAYED YET")
        self.backend_label.setText(backend_label(str(profile.get("backend") or "")))
        self._set_page_notice(str(profile.get("backend") or ""))
        self.version_label.setText("")
        self.hero.set_source(None)
        self.banners.set_banners([])
        self.console.clear()
        self.step_label.setText("")
        self.elapsed_label.setText("")
        self.progress.setVisible(False)
        self.progress.setValue(0)
        self._set_badge("checking", "")
        self._set_detail("")
        self._hide_qr()
        self._clear_news()
        self._set_console_expanded(True)

        self._news_fallback = list(profile_art_candidates(profile))
        self._load_profile_art(self._news_fallback, title)
        if snapshot is not None:
            if snapshot.status is not None:
                self._apply_status(snapshot.status)
            if snapshot.news is not None:
                self.set_news(snapshot.news)
            if snapshot.media is not None:
                self.set_media(
                    snapshot.media.get("hero"), snapshot.media.get("banners") or []
                )
            if snapshot.qr:
                self._show_qr(snapshot.qr)
        self._refresh_actions()

    def _load_profile_art(self, candidates: list[str], title: str) -> None:
        """Decode the profile cover off the GUI thread; placeholder until ready."""
        seq = self._profile_seq
        self.art_label.setPixmap(cover_pixmap(None, 200, 256, title, radius=9))

        def apply(pixmap: QPixmap | None) -> None:
            if seq != self._profile_seq:
                return  # the user already switched away
            try:
                self.art_label.setPixmap(cover_pixmap(pixmap, 200, 256, title, radius=9))
            except RuntimeError:
                return  # the widget was destroyed while the decode was pending

        self.images.request(candidates, 200, 256, apply, crop=True, cover=True)

    @property
    def profile(self) -> str:
        return self._profile

    def set_media(self, hero_path: str | None, banners: list[dict] | None = None) -> None:
        """Apply official media: a full-bleed hero background and banners.

        Called after the background media worker finishes; an empty result
        leaves today's plain hero card in place. The hero is decoded off the
        GUI thread and only applied if the page still shows this profile.
        """
        self._media_seq += 1
        seq = self._media_seq
        if hero_path:

            def apply(pixmap: QPixmap | None) -> None:
                if seq != self._media_seq:
                    return
                try:
                    self.hero.set_source(pixmap)
                except RuntimeError:
                    return  # the widget was destroyed while the decode was pending

            self.images.request([str(hero_path)], 1280, 300, apply, crop=True)
        else:
            self.hero.set_source(None)
        self.banners.set_banners(list(banners or []))

    def wait_for_images(self, timeout_ms: int = 5000) -> None:
        """Block until scheduled image decodes have delivered their callbacks."""
        self.images.wait(timeout_ms)

    # ---- status ------------------------------------------------------------
    def set_status(self, status: Status) -> None:
        self._apply_status(status)
        self._refresh_actions()

    def _apply_status(self, status: Status) -> None:
        self._status = status
        if status.running or status.state == "running":
            self.kicker.setText("NOW PLAYING")
        elif status.state == "not_installed":
            self.kicker.setText("NOT PLAYED YET")
        else:
            self.kicker.setText("GAME")
        self._set_badge(status.state, status.message)
        self.version_label.setText(_version_text(status.version))
        self._set_account(status)
        self._set_detail(status.message or "")

    def _set_account(self, status: Status) -> None:
        """Say whether the account is logged in, for backends that track it."""
        data = status.data or {}
        known = "logged_in" in data and status.state != "not_installed"
        self.account_label.setVisible(known)
        if known:
            text = "Logged in" if data["logged_in"] else "Not logged in"
            account = data.get("account")
            if data["logged_in"] and isinstance(account, str) and account:
                text += f" as {account}"
            self.account_label.setText(text)

    def _set_badge(self, state: str, message: str = "") -> None:
        pill_state, label = badge_style(state)
        self.badge.setText(label)
        set_state_property(self.badge, pill_state)
        self.badge.setToolTip(message or "")

    def _set_detail(self, text: str) -> None:
        self.detail_label.setText(text)

    # ---- primary action ----------------------------------------------------
    def _primary_op(self) -> str | None:
        if self._busy_op is not None:
            return None
        status = self._status
        if status is None:
            return None
        if status.running or status.state == "running":
            return "stop"
        if status.state == "error":
            return "retry"
        if status.state == "not_installed":
            if (status.data or {}).get("can_install") is False:
                return "settings"
            return "install"
        data = status.data or {}
        if data.get("update_available"):
            return "update"
        if data.get("login_required") and not data.get("logged_in"):
            return "login"
        return "launch"

    def _refresh_actions(self) -> None:
        if self._busy_op is not None:
            # A launch that is busy means the game process is up: say so and
            # offer Stop; every other busy operation is a cancellable one.
            running = self._busy_op == "launch"
            self.primary.setText("STOP" if running else "CANCEL")
            self.primary.setObjectName("Danger")
            self.primary.setEnabled(True)
            self._polish(self.primary)
            for button in (
                self.login,
                self.logout,
                self.update_btn,
                self.verify,
                self.repair_btn,
            ):
                button.setEnabled(False)
            return

        op = self._primary_op()
        self.primary.setObjectName("Primary")
        self._polish(self.primary)
        if op is None:
            # No status yet: a neutral, disabled skeleton, never CANCEL.
            self.primary.setText(PRIMARY_LABELS["launch"])
            self.primary.setEnabled(False)
        else:
            self.primary.setText(PRIMARY_LABELS.get(op, op.upper()))
            # Even "SET GAME EXE IN SETTINGS" is a real action: it opens Settings.
            self.primary.setEnabled(True)

        status = self._status
        installed = bool(status and status.state == "installed")
        running = bool(status and (status.running or status.state == "running"))
        self.login.setEnabled(installed and not running)
        # Log out only needs an idle account: it must be reachable even when the
        # game is not installed (e.g. to drop a saved login).
        self.logout.setEnabled(bool(status) and not running)
        can_update = supports_update(str(self._profile_data.get("backend") or ""))
        if ((status.data if status else {}) or {}).get("can_update") is False:
            can_update = False
        self.update_btn.setEnabled(installed and not running and can_update)
        self.update_btn.setToolTip(
            ""
            if can_update
            else "Nadare cannot update this game; update it in Steam or its own launcher."
        )
        self.verify.setEnabled(
            bool(status) and not running and status.state not in ("error", "not_installed")
        )
        can_repair = bool(((status.data if status else {}) or {}).get("can_repair"))
        self.repair_btn.setEnabled(self.verify.isEnabled() and can_repair)

    def set_account_controls(self, supported: bool) -> None:
        """Show account controls only for backends that log in."""
        self.login.setVisible(supported)
        self.logout.setVisible(supported)
        self._refresh_actions()

    def set_remember(self, supported: bool, checked: bool) -> None:
        """Show Remember me (page + QR card) for backends that support it."""
        for box in (self.remember, self.qr_frame.remember):
            box.blockSignals(True)
            box.setVisible(bool(supported))
            box.setChecked(bool(checked))
            box.blockSignals(False)

    def _on_remember_clicked(self, checked: bool) -> None:
        # Keep the page and QR-card boxes in step, then tell the window.
        for box in (self.remember, self.qr_frame.remember):
            box.blockSignals(True)
            box.setChecked(bool(checked))
            box.blockSignals(False)
        self.remember_toggled.emit(bool(checked))

    @staticmethod
    def _polish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_primary(self) -> None:
        if self._busy_op is not None:
            if self._busy_op == "launch":
                # The button reads STOP: run the backend's stop operation rather
                # than the generic cancel path (which is not backend-specific).
                self.action_requested.emit("stop")
            elif time.monotonic() < self._primary_grace_until:
                # The first click of a double-click already started the job; the
                # button turned into CANCEL under the user's finger.
                return
            else:
                self.cancel_requested.emit()
            return
        op = self._primary_op()
        if op == "retry":
            self.action_requested.emit("status")
        elif op == "settings":
            self.settings_requested.emit()
        elif op:
            self.action_requested.emit(op)

    # ---- lifecycle ---------------------------------------------------------
    def set_busy(self, operation: str) -> None:
        """Mark ``operation`` as running and start the elapsed clock."""
        self._busy_op = operation
        self._primary_grace_until = (
            time.monotonic() + _CANCEL_GRACE_S if operation != "launch" else 0.0
        )
        self.news_frame.setVisible(False)
        self._started_at = time.time()
        self.progress.setRange(0, 100)
        self.progress.setVisible(operation != "launch")
        self.progress.setValue(0)
        self.step_label.setText("Running" if operation == "launch" else operation.capitalize())
        self.elapsed_label.setText("·  0:00")
        self._set_badge(
            "running" if operation == "launch" else _BUSY_STATES.get(operation, "installing"), ""
        )
        self._hide_qr()
        self._timer.start()
        self._set_console_expanded(True)
        self._refresh_actions()

    def _clear_busy_progress(self) -> None:
        """Return the progress area to its idle shape when an operation ends."""
        self.progress.setRange(0, 100)  # never leave the busy animation running
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.step_label.setText("")
        self.elapsed_label.setText("")
        self._started_at = 0.0

    def finish(self, operation: str, exit_code: int) -> None:
        """Stop the clock and report the result."""
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._tick()
        self._restore_badge()
        self._refresh_actions()
        if operation == "launch" and exit_code == 0:
            self._append_line("game exited normally", "done")
            return
        if exit_code != 0:
            self.show_error(
                "Operation failed",
                f"{operation_title(operation)} did not finish (exit code {exit_code}). "
                "Open the logs for details.",
            )

    def fail(self, message: str) -> None:
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._restore_badge()
        self._refresh_actions()
        self.show_error("Backend error", message)

    def cancelled(self) -> None:
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._restore_badge()
        self._refresh_actions()
        self._append_line("cancelled", "status")

    def _restore_badge(self) -> None:
        """Drop the busy pill once an operation ends without a status refresh."""
        if self._status is not None:
            self._set_badge(self._status.state, self._status.message)
        else:
            self._set_badge("checking", "")

    def _tick(self) -> None:
        if not self._started_at:
            return
        elapsed = int(time.time() - self._started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_label.setText(f"·  {minutes}:{seconds:02d}")

    # ---- events ------------------------------------------------------------
    def append_event(self, event: Event) -> None:
        kind = event.kind
        if kind == "step":
            self.step_label.setText(event.message or "")
            data = event.data or {}
            png = data.get("png")
            url = data.get("url")
            if not self._show_qr(png) and url:
                self._show_qr_link(str(url))
            if data.get("qr_state"):
                self._set_qr_state(str(data["qr_state"]))
            if event.progress is not None:
                self._set_progress(event.progress)
        elif kind == "progress":
            if (event.data or {}).get("indeterminate"):
                # Busy bar: the phase has no measurable progress (silent installer).
                self.progress.setVisible(True)
                self.progress.setRange(0, 0)
            elif event.progress is not None:
                self._set_progress(event.progress)
            if event.message:
                self.step_label.setText(event.message)
        elif kind == "status":
            if event.message:
                self._set_detail(event.message)
        elif kind == "done":
            self._hide_qr()
            self._append_line(event.message or "done", "done")
        elif kind == "error":
            self._hide_qr()
            self._set_console_expanded(True)
            self._append_line(event.message, "error")
        else:
            self._append_line(event.message, kind)
        if event.progress is not None and kind not in ("step", "progress"):
            self._set_progress(event.progress)

    def _set_progress(self, value: float) -> None:
        self.progress.setRange(0, 100)  # leave the busy (indeterminate) state
        self.progress.setVisible(True)
        self.progress.setValue(max(0, min(100, int(round(value * 100)))))

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

    # ---- news ---------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if getattr(self, "qr_frame", None) is not None and not self.qr_frame.isHidden():
            self.qr_frame.reposition()
        if getattr(self, "news_grid", None) is None:
            return
        columns = self._news_columns()
        if columns != self.news_columns:
            self.news_columns = columns
            self._relayout_news()

    def _news_columns(self) -> int:
        """Four columns for a very wide window, three for a wide one, two for a
        medium one, else one.

        The news grid sits inside the page next to the fixed-width navigation
        sidebar, so the breakpoints are applied to the surrounding window rather
        than to the slightly narrower grid viewport.
        """
        window = self.window()
        width = window.width() if window is not None else self.width()
        if width >= 1700:
            return 4
        if width >= 1000:
            return 3
        if width >= 640:
            return 2
        return 1

    def _relayout_news(self) -> None:
        """Reflow the news cards into the responsive grid."""
        while self.news_grid.count():
            self.news_grid.takeAt(0)
        self.news_columns = self._news_columns()
        for column in range(8):
            self.news_grid.setColumnStretch(column, 0)
        for column in range(self.news_columns):
            self.news_grid.setColumnStretch(column, 1)
        for index, card in enumerate(self.news_rows):
            row, column = divmod(index, self.news_columns)
            self.news_grid.addWidget(card, row, column, Qt.AlignmentFlag.AlignTop)

    def set_news(self, result: Any) -> None:
        """Render up to eight official news items from a ``NewsResult``."""
        items = list(getattr(result, "items", []) or [])[:8]
        available = bool(getattr(result, "available", True))
        key = (
            self._profile,
            available,
            tuple(
                (item.get("id"), item.get("title"), item.get("image_path"), item.get("url"))
                for item in items
            ),
        )
        if items and self.news_rows and key == self._news_key:
            return  # a silent refresh with the same items: keep the cards, no flash
        self._clear_news()
        self._news_key = key
        for item in items:
            card = NewsCard(
                item, self.news_frame, loader=self.images, fallback=self._news_fallback
            )
            card.clicked.connect(self._open_external)
            self.news_rows.append(card)
        if self.news_rows:
            self.news_hint.setVisible(False)
            self.news_reason.setVisible(False)
            self.news_retry.setVisible(False)
            self._set_news_visible(True)
            self._relayout_news()
            if self._busy_op is None:
                self._set_console_expanded(False)
        else:
            has_feed = supports_news(str(self._profile_data.get("backend") or ""))
            self.news_hint.setText("News unavailable")
            self.news_hint.setVisible(True)
            self.news_reason.setText(
                "There is no news to show right now."
                if available
                else "Nadare could not reach the publisher's news feed. "
                "Check your connection, then try again."
            )
            self.news_reason.setVisible(True)
            self.news_retry.setVisible(has_feed)
            # Nothing to show: hide the section entirely only when the backend
            # has no feed at all; otherwise keep the reason and Retry, plus the log.
            self._set_news_visible(available or has_feed)
            self._set_console_expanded(True)

    def _set_news_visible(self, shown: bool) -> None:
        """Remember the wanted visibility; the section stays hidden during an
        operation so the progress bar and output get the room."""
        self._news_shown = bool(shown)
        self.news_frame.setVisible(self._news_shown and self._busy_op is None)

    def _clear_news(self) -> None:
        for card in self.news_rows:
            card.setParent(None)
            card.deleteLater()
        self.news_rows = []
        self._news_key = None
        if getattr(self, "news_grid", None) is not None:
            while self.news_grid.count():
                self.news_grid.takeAt(0)
        self.news_hint.setVisible(True)
        self.news_reason.clear()
        self.news_reason.setVisible(False)
        self.news_retry.setVisible(False)
        self._set_news_visible(False)

    def _set_console_expanded(self, expanded: bool) -> None:
        self._console_expanded = bool(expanded)
        self.console.setVisible(self._console_expanded)
        self.console_toggle.setText("Hide output" if self._console_expanded else "Show output")

    def _toggle_console(self) -> None:
        self._set_console_expanded(not self._console_expanded)

    def _open_external(self, url: str) -> None:
        if not url:
            return
        qurl = QUrl(url)
        if qurl.scheme().lower() not in ("http", "https") or not qurl.host():
            return
        QDesktopServices.openUrl(qurl)

    def _append_line(self, message: str, kind: str = "log") -> None:
        if not message:
            return
        color = _LOG_COLORS.get(kind, _LOG_COLORS["log"])
        safe = html.escape(str(message)).replace("\n", "<br>")
        self.console.appendHtml(f'<span style="color:{color}">{safe}</span>')
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---- dialogs -----------------------------------------------------------
    def show_error(self, title: str, message: str) -> None:
        self._set_console_expanded(True)
        if userns.has_hint(message):
            UsernsDialog(message, self).exec()
        else:
            QMessageBox.warning(self, title, message)

    def _open_logs(self) -> None:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))


__all__ = [
    "BannerStrip",
    "ElidedLabel",
    "GameSnapshot",
    "GameView",
    "HeroCard",
    "NewsCard",
    "NewsRow",
    "PRIMARY_LABELS",
]
