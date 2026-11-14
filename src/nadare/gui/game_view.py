# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-game page: hero header, status, the one primary action, progress and log.

This widget renders state and forwards user intent as signals; it never calls a
backend itself. The main window owns the :class:`BackendController` and wires
those signals to workers.
"""

from __future__ import annotations

import html
import time as time  # noqa: E402, F401
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
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
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

# Names that tests and callers reach through this module; the code using them moved to
# game_view_parts/ (#1248). Re-exported so ``game_view.X`` is the same object.
from PyQt6.QtWidgets import QMessageBox as QMessageBox  # noqa: E402, F401

from ..core.backends import (
    Status,
    install_choice_label,
    page_notice,
    page_summary,
)
from ..core.eta import EtaEstimator, SpeedMeter
from ..core.news import CREDIT
from .game_view_parts.busy import (
    _ANNOUNCED_ON_SUCCESS,
    _BUSY_STATES,
    _CANCEL_GRACE_S,
    _GAME_END_LINES,
    _LAUNCH_EXIT_GRACE_S,
    _PAUSED_TEXT,
    _RETRYABLE,
    _STATE_CHANGING,
    OPERATION_TITLES,
    BusyMixin,
    can_pause_kind,
    operation_title,
)
from .game_view_parts.console import (
    _LOG_COLORS,
    ConsoleMixin,
)
from .game_view_parts.news import (
    NewsMixin,
)
from .game_view_parts.qr import (
    QrMixin,
)
from .game_view_parts.shared import (
    _NEWS_THUMB_MAX_H,
    _QR_SCAN_HINT,
    _REMEMBER_TEXT,
    _REMEMBER_TIP,
    _SANDBOX_TIP,
    ElidedLabel,
    NewsCard,
    _format_news_date,
    _make_keyboard_link,
    _QrPanel,
    _ThumbnailLabel,
)
from .game_view_parts.status import (
    PRIMARY_LABELS,
    StatusMixin,
    _version_text,
)
from .game_view_parts.switches import (
    SwitchesMixin,
)
from .images import ImageLoader
from .library import FlowLayout, backend_label, profile_art_candidates, profile_title
from .theme import cover_pixmap, set_state_property

#: Backends whose label ("Steam game", "Wine / Proton game") adds nothing on the game page.
_SELF_EVIDENT_BACKENDS = frozenset({"steam", "wine"})


@dataclass
class GameSnapshot:
    """Last rendered state for one profile, replayed instantly on a switch."""

    status: Status | None = None
    news: Any = None
    media: dict | None = None
    qr: str | None = None


#: Operation banner height (px).
_BANNER_H = 110

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
        #: ``(source key, width, height)`` and the cropped pixmap painted for it.
        self._cropped_key: tuple[int, int, int] | None = None
        self._cropped: QPixmap | None = None

    def set_source(self, source: QPixmap | None) -> None:
        self._source = source if (source is not None and not source.isNull()) else None
        self._cropped_key = None
        self._cropped = None
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
        key = (self._source.cacheKey(), width, height)
        if self._cropped is None or self._cropped_key != key:
            scaled = self._source.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width() - width) // 2)
            y = max(0, (scaled.height() - height) // 2)
            self._cropped = scaled.copy(x, y, width, height)
            self._cropped_key = key
        painter.drawPixmap(0, 0, self._cropped)
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
            _make_keyboard_link(self, "QLabel#Banner", "Open the announcement in your browser")
        # A fixed placeholder keeps the strip from jumping while the decode runs.
        self.setFixedSize(round(_BANNER_H * 16 / 9), _BANNER_H)
        if loader is not None:
            loader.request([str(image_path)], 2000, _BANNER_H, self._apply, crop=False)
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

class BannerStrip(QWidget):
    """A slim horizontally-scrolling strip of official operation banners."""

    clicked = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None, loader: ImageLoader | None = None) -> None:
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


class _NotifyLabel(QLabel):
    """A label that says when its text changed, so the box around it can hide when empty."""

    text_changed = pyqtSignal()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self.text_changed.emit()

    def clear(self) -> None:
        super().clear()
        self.text_changed.emit()


#: Backwards-compatible alias for the old single-row widget name.
NewsRow = NewsCard


class GameView(BusyMixin, ConsoleMixin, NewsMixin, QrMixin, StatusMixin, SwitchesMixin, QWidget):
    """The single-game launch page."""

    action_requested = pyqtSignal(str)  # operation name
    notice_preference_changed = pyqtSignal(bool)  # "How this game runs" opened or closed
    console_preference_changed = pyqtSignal(bool)  # the user pressed Show/Hide output
    cancel_requested = pyqtSignal()
    skip_step_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    queue_next_requested = pyqtSignal()
    queue_now_requested = pyqtSignal()
    predownload_requested = pyqtSignal()
    predownload_cancel_requested = pyqtSignal()
    back_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    move_requested = pyqtSignal(str)  # profile name
    remove_requested = pyqtSignal(str)  # profile name
    report_requested = pyqtSignal(str)
    found_copy_requested = pyqtSignal(str)  # profile name: use a copy found on disk (#1293)
    steam_setup_requested = pyqtSignal()
    remember_toggled = pyqtSignal(bool)  # the user ticked/unticked Remember me
    sandbox_toggled = pyqtSignal(bool)  # the user turned the Sandbox on or off

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
        self._sandbox_supported = False
        self._sandbox_blocked = ""
        self._sandbox_ready = True
        #: Whether the running launch was started sandboxed (None when not running or unknown).
        self._sandbox_active: bool | None = None
        self._paused_op: str | None = None
        self._failed_op: str | None = None  # what RETRY runs again after a failure
        self._found_copy: str | None = None  # a folder the start-up check found (#1293)
        self._queue_waiting = False  # waits for an install slot (#770)
        #: True between a successful install/update/... and the status that
        #: follows it, so the stale status cannot offer the same action again.
        self._awaiting_status = False
        self._primary_grace_until = 0.0
        self._news_shown = False  # intended news visibility (hidden while busy)
        self._started_at = 0.0
        self._last_step_at = 0.0  # last phase step; play time is counted from it
        # OUTPUT starts folded (#993): it is noise unless something fails. The user's own
        # Show/Hide choice is kept for every game and operation (the window saves it).
        self._console_preference = False
        self._console_expanded = False
        self._last_done = ""  # the running operation's "done" line, said again when it ends
        self._last_line = ""  # the console's newest line, so a repeated check error shows once
        # Something the user asked for (a RETRY of a check) may fail with the same words again:
        # that failure is news and is shown (round 5).
        self.action_requested.connect(self._forget_last_line)
        self.news_columns = 1
        self._profile_seq = 0
        self._media_seq = 0
        self._predownload_version = ""
        self._predownload_ready = False
        self._predownload_running = False
        self._predownload_fraction: float | None = None

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
        # Move and Remove used to be three levels down in Settings.
        self.more_button = QPushButton("\u22ef")
        self.more_button.setObjectName("GhostButton")
        self.more_button.setAccessibleName("More")
        self.more_button.setToolTip("More: move or remove this game")
        self.more_menu = QMenu(self.more_button)
        self.more_menu.addAction("Move game…").triggered.connect(
            lambda: self.move_requested.emit(self._profile)
        )
        self.more_menu.addAction("Uninstall or remove…").triggered.connect(
            lambda: self.remove_requested.emit(self._profile)
        )
        self.more_button.setMenu(self.more_menu)
        header.addWidget(self.more_button)
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

        meta_row = FlowLayout(spacing=9)  # wraps in a narrow window, like the buttons below
        self.badge = QLabel("")
        self.badge.setObjectName("Pill")
        meta_row.addWidget(self.badge)
        self.sandbox_pill = QLabel("SANDBOX")
        self.sandbox_pill.setObjectName("Pill")
        set_state_property(self.sandbox_pill, "info")
        self.sandbox_pill.setToolTip(_SANDBOX_TIP)
        self.sandbox_pill.setVisible(False)
        meta_row.addWidget(self.sandbox_pill)
        self.backend_label = QLabel("")
        self.backend_label.setObjectName("Muted")
        meta_row.addWidget(self.backend_label)
        self.version_label = QLabel("")
        self.version_label.setObjectName("Dim")
        meta_row.addWidget(self.version_label)
        # The resource quality a game was installed with (WuWa SD/HD/UHD, #990).
        self.choice_label = QLabel("")
        self.choice_label.setObjectName("Dim")
        self.choice_label.setToolTip("Change it in Settings → Game options.")
        self.choice_label.setVisible(False)
        meta_row.addWidget(self.choice_label)
        self.account_label = QLabel("")
        self.account_label.setObjectName("Muted")
        self.account_label.setVisible(False)
        meta_row.addWidget(self.account_label)
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
        # Internal links fold the notice (#992); outside ones go through _open_external.
        self.notice_label.setOpenExternalLinks(False)
        self.notice_label.linkActivated.connect(self._on_notice_link)
        self.notice_label.setVisible(False)
        self._notice_kind = ""
        self._notice_open: bool | None = None  # None: whole before install, short after
        copy.addWidget(self.notice_label)
        copy.addStretch(1)

        self.primary = QPushButton("LAUNCH")
        self.primary.setObjectName("Primary")
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.primary.setMinimumHeight(52)
        self.primary.clicked.connect(self._on_primary)
        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)
        primary_row.addWidget(self.primary, 1)
        # Like Steam: the big button says PLAYING and a small x ends the session.
        self.stop_button = QPushButton("\u2715")
        self.stop_button.setObjectName("GhostButton")
        self.stop_button.setAccessibleName("Stop game")
        self.stop_button.setToolTip("Stop game")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setMinimumHeight(52)
        self.stop_button.setMinimumWidth(52)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.stop_button.setVisible(False)
        primary_row.addWidget(self.stop_button)
        copy.addLayout(primary_row)

        # An icon, not a word: the pause symbol reads the same in every language.
        self.pause_button = QPushButton()
        self.pause_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        self.pause_button.setIconSize(QSize(22, 22))
        self.pause_button.setAccessibleName("Pause")
        self.pause_button.setObjectName("GhostButton")
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.setToolTip(
            "Stop for now. Everything downloaded so far is kept, and Resume carries on from there."
        )
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.pause_button.setVisible(False)
        copy.addWidget(self.pause_button)

        # A paused download has no CANCEL on the primary (it reads RESUME): give it its own way out.
        self.cancel_paused_button = QPushButton("Stop download")
        self.cancel_paused_button.setObjectName("GhostButton")
        self.cancel_paused_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_paused_button.setToolTip(
            "Stop this download instead of resuming it. What is already downloaded stays on disk."
        )
        self.cancel_paused_button.clicked.connect(self.cancel_requested.emit)
        self.cancel_paused_button.setVisible(False)
        copy.addWidget(self.cancel_paused_button)

        # Only while the install waits for a free slot: no dragging, just two plain choices.
        self.queue_row = QWidget()
        queue_layout = QHBoxLayout(self.queue_row)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(8)
        self.queue_next_btn = QPushButton("Download next")
        self.queue_next_btn.setObjectName("GhostButton")
        self.queue_next_btn.setToolTip("Move this game to the front of the waiting line.")
        self.queue_next_btn.clicked.connect(self.queue_next_requested.emit)
        self.queue_now_btn = QPushButton("Download now")
        self.queue_now_btn.setObjectName("GhostButton")
        self.queue_now_btn.setToolTip(
            "Pause the download that is running, start this one, and carry on with the other "
            "afterwards. Nothing already downloaded is lost."
        )
        self.queue_now_btn.clicked.connect(self.queue_now_requested.emit)
        queue_layout.addWidget(self.queue_next_btn)
        queue_layout.addWidget(self.queue_now_btn)
        queue_layout.addStretch(1)
        self.queue_row.setVisible(False)
        copy.addWidget(self.queue_row)

        # Wraps onto a second line in a narrow window: a single row (up to eight buttons) beside
        # the cover made the page wider than the smallest window, and the header's Settings and
        # More buttons (Remove, Move) fell off the right edge (b23-rc2 click-through).
        secondary = FlowLayout(spacing=8)
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
        self.predownload_btn = QPushButton()
        self.predownload_btn.setObjectName("GhostButton")
        self.predownload_btn.clicked.connect(self._on_predownload_clicked)
        self.predownload_btn.setVisible(False)
        self.verify = QPushButton("Check game files")
        self.verify.setObjectName("GhostButton")
        self.verify.setToolTip(
            "Compares the installed game files with the official list. "
            "Does not change or download anything."
        )
        self._verify_tip = self.verify.toolTip()
        self.verify.clicked.connect(lambda: self.action_requested.emit("verify"))
        self.repair_btn = QPushButton("Repair files")
        self.repair_btn.setObjectName("GhostButton")
        self.repair_btn.setToolTip(
            "Re-check every game file and re-download the ones that are missing or damaged"
        )
        self._repair_tip = self.repair_btn.toolTip()
        self.repair_btn.clicked.connect(lambda: self.action_requested.emit("repair"))
        self.steam_setup_btn = QPushButton("Set up")
        self.steam_setup_btn.setObjectName("GhostButton")
        self.steam_setup_btn.setToolTip(
            "Checks this Steam game and fixes what is missing: video support, a prefix "
            "component, an optional runner. Asks before changing anything."
        )
        self.steam_setup_btn.setVisible(False)
        self.steam_setup_btn.clicked.connect(self.steam_setup_requested.emit)
        self.logs = QPushButton("Open logs")
        self.logs.setObjectName("GhostButton")
        self.logs.clicked.connect(self._open_logs)
        self.report = QPushButton("Report a problem")
        self.report.setObjectName("GhostButton")
        self.report.clicked.connect(lambda: self.report_requested.emit(self._profile))
        for button in (
            self.login,
            self.logout,
            self.update_btn,
            self.predownload_btn,
            self.verify,
            self.repair_btn,
            self.steam_setup_btn,
            self.logs,
            self.report,
        ):
            secondary.addWidget(button)
        copy.addLayout(secondary)

        self.remember = QCheckBox(_REMEMBER_TEXT)
        self.remember.setToolTip(_REMEMBER_TIP)
        self.remember.setVisible(False)
        self.remember.clicked.connect(self._on_remember_clicked)
        copy.addWidget(self.remember)

        self.sandbox = QCheckBox("Sandbox: hide my home folder")
        self.sandbox.setToolTip(_SANDBOX_TIP)
        self.sandbox.setVisible(False)
        self.sandbox.clicked.connect(self._on_sandbox_clicked)
        copy.addWidget(self.sandbox)

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
        self.step_label = _NotifyLabel("")
        self.step_label.setObjectName("Hint")
        self.step_label.text_changed.connect(self._sync_progress_frame)
        self.step_label.setWordWrap(True)
        step_row.addWidget(self.step_label, 1)
        self.skip_button = QPushButton("Skip this step")
        self.skip_button.setObjectName("GhostButton")
        self.skip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_button.setToolTip(
            "Stops the installer and unpacks the component's files instead. The launch carries on."
        )
        self.skip_button.setHidden(True)
        self.skip_button.clicked.connect(self._on_skip_clicked)
        step_row.addWidget(self.skip_button, 0, Qt.AlignmentFlag.AlignRight)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("Muted")
        step_row.addWidget(self.elapsed_label, 0, Qt.AlignmentFlag.AlignRight)
        progress_layout.addLayout(step_row)
        self.progress = QProgressBar()
        self._eta = EtaEstimator()
        self._speed = SpeedMeter()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        progress_layout.addWidget(self.progress)
        self.progress_frame = progress_frame
        root.addWidget(progress_frame)
        self._sync_progress_frame()

        # ---- QR (STOVE login only) -----------------------------------------
        # The auth worker emits a step event whose data carries the QR png path;
        # it is shown as an in-app overlay card, centred over the page.
        self.qr_frame = _QrPanel(self)
        self.qr_label = self.qr_frame.label
        self.qr_hint = self.qr_frame.hint
        self.qr_frame.cancel_clicked.connect(self.cancel_requested.emit)
        self.qr_frame.remember_clicked.connect(self._on_remember_clicked)

        # ---- console --------------------------------------------------------
        self.output_head = QWidget()
        log_head = QHBoxLayout(self.output_head)
        log_head.setContentsMargins(0, 0, 0, 0)
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
        clear.clicked.connect(self._clear_console)
        log_head.addWidget(clear)
        root.addWidget(self.output_head)

        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setPlaceholderText("Nadare's progress messages will appear here…")
        root.addWidget(self.console, 1)

        self._set_console_expanded(self._console_preference)
        self._refresh_actions()

    def _set_page_notice(self, kind: str) -> None:
        self._notice_kind = kind
        self._render_notice()

    def _render_notice(self) -> None:
        """The backend's notice: whole before the game is installed, one line after (#992)."""
        notice = page_notice(self._notice_kind)
        if not notice:
            self.notice_label.clear()
            self.notice_label.setVisible(False)
            return
        text, url = notice[0], notice[1]
        label = notice[2] if len(notice) > 2 else "Learn more"
        summary = page_summary(self._notice_kind)
        foldable = bool(summary) and summary != text.strip()
        status = getattr(self, "_status", None)
        installed = status is not None and status.state != "not_installed"
        whole = not foldable or (
            self._notice_open if self._notice_open is not None else not installed
        )
        link = f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        if not foldable:
            body = f"{html.escape(text)} {link}"
        elif whole:
            body = f'{html.escape(text)} <a href="nadare:less">Show less</a> · {link}'
        else:
            body = f'{html.escape(summary)} <a href="nadare:more">How this game runs</a> · {link}'
        self.notice_label.setText(body)
        self.notice_label.setVisible(True)

    def _on_notice_link(self, href: str) -> None:
        if href in ("nadare:more", "nadare:less"):
            self._notice_open = href == "nadare:more"
            self.notice_preference_changed.emit(self._notice_open)
            self._render_notice()
            return
        self._open_external(href)

    def set_notice_preference(self, expanded: bool | None) -> None:
        """The saved "How this game runs" choice; ``None`` lets the install state decide."""
        self._notice_open = expanded
        self._render_notice()

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
        self._found_copy = None  # an offer for one game never follows to the next
        # Until this game's status lands, the previous game's predownload button must not show.
        self._predownload_version = ""
        self._predownload_ready = False
        self._predownload_running = False
        self._predownload_fraction = None
        self.predownload_btn.setVisible(False)
        # Switching games ends the previous game's operation on this page. The
        # timer/elapsed clock belonged to that page state; MainWindow restores
        # set_busy below when the *new* profile is itself running an operation.
        self._timer.stop()
        self._started_at = 0.0
        self._last_step_at = 0.0
        self._busy_op = None
        self._sandbox_active = None
        self._paused_op = None
        self._failed_op = None
        self._awaiting_status = False
        self._primary_grace_until = 0.0
        title = profile_title(name, profile)
        self.title.setText(title)
        self.kicker.setText("NOT PLAYED YET")
        self.kicker.setVisible(False)
        kind = str(profile.get("backend") or "")
        self.backend_label.setText(backend_label(kind))
        # "Steam game" and "Wine / Proton game" say what the page already shows.
        self.backend_label.setVisible(kind.strip().lower() not in _SELF_EVIDENT_BACKENDS)
        self._set_page_notice(str(profile.get("backend") or ""))
        self.version_label.setText("")
        choice = install_choice_label(kind, profile)
        self.choice_label.setText(choice)
        self.choice_label.setVisible(bool(choice))
        self.hero.set_source(None)
        self.banners.set_banners([])
        self.console.clear()
        self._last_line = ""
        self.step_label.setText("")
        self.elapsed_label.setText("")
        self.progress.setVisible(False)
        self.progress.setValue(0)
        self._set_badge("checking", "")
        self._set_detail("")
        # Until this game's status lands, the previous game's "Logged in as …" must not show.
        self.account_label.setText("")
        self.account_label.setVisible(False)
        self._hide_qr()
        self._clear_news()
        self._set_console_expanded(self._console_preference)

        self._news_fallback = list(profile_art_candidates(profile))
        self._load_profile_art(self._news_fallback, title)
        if snapshot is not None:
            if snapshot.status is not None:
                self._apply_status(snapshot.status)
            if snapshot.news is not None:
                self.set_news(snapshot.news)
            if snapshot.media is not None:
                self.set_media(snapshot.media.get("hero"), snapshot.media.get("banners") or [])
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

    def set_predownload_progress(
        self, active: bool, fraction: float | None = None
    ) -> None:
        """Show a predownload running beside the normal page, without disabling Play."""
        self._predownload_running = active
        self._predownload_fraction = fraction if active else None
        self._refresh_predownload_button()

    def _on_predownload_clicked(self) -> None:
        if self._predownload_running:
            self.predownload_cancel_requested.emit()
        else:
            self.predownload_requested.emit()

    def _refresh_predownload_button(self) -> None:
        """Pick the one of four states the button can have, or hide it when there is none."""
        version = self._predownload_version
        if self._predownload_running:
            text = f"Predownloading {version}…" if version else "Predownloading…"
            if self._predownload_fraction is not None:
                percent = int(self._predownload_fraction * 100)
                text = f"{text} {percent}%"
            self.predownload_btn.setText(text)
            self.predownload_btn.setToolTip(
                "Click to stop. What is downloaded so far is kept."
            )
            self.predownload_btn.setEnabled(True)
            self.predownload_btn.setVisible(True)
        elif not version:
            self.predownload_btn.setVisible(False)
        elif self._predownload_ready:
            self.predownload_btn.setText(f"{version} predownloaded")
            self.predownload_btn.setToolTip(
                f"Version {version} is downloaded. The update on release day will use it."
            )
            self.predownload_btn.setEnabled(False)
            self.predownload_btn.setVisible(True)
        else:
            self.predownload_btn.setText(f"Predownload {version}")
            self.predownload_btn.setToolTip(
                f"Download version {version} now, before it is released. "
                "The game stays playable; on release day the update uses these files."
            )
            self.predownload_btn.setEnabled(True)
            self.predownload_btn.setVisible(True)

    @staticmethod
    def _polish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

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

    def _open_external(self, url: str) -> None:
        if not url:
            return
        qurl = QUrl(url)
        if qurl.scheme().lower() not in ("http", "https") or not qurl.host():
            return
        QDesktopServices.openUrl(qurl)

    def _forget_last_line(self, *_args: object) -> None:
        self._last_line = ""

__all__ = [
    "BannerStrip",
    "ElidedLabel",
    "GameSnapshot",
    "GameView",
    "HeroCard",
    "NewsCard",
    "NewsRow",
    "PRIMARY_LABELS",
    "OPERATION_TITLES",
    "operation_title",
    "_BUSY_STATES",
    "_CANCEL_GRACE_S",
    "_LAUNCH_EXIT_GRACE_S",
    "_PAUSED_TEXT",
    "_STATE_CHANGING",
    "_ANNOUNCED_ON_SUCCESS",
    "_GAME_END_LINES",
    "_RETRYABLE",
    "can_pause_kind",
    "_LOG_COLORS",
    "PRIMARY_LABELS",
    "_version_text",
    "_NEWS_THUMB_MAX_H",
    "_QR_SCAN_HINT",
    "_make_keyboard_link",
    "_format_news_date",
    "ElidedLabel",
    "_ThumbnailLabel",
    "NewsCard",
    "_REMEMBER_TEXT",
    "_REMEMBER_TIP",
    "_SANDBOX_TIP",
    "_QrPanel",
]
