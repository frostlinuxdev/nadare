# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The library: compact covers, recent games and favourites.

The library is presentation only. It renders whatever profiles the config holds
and shows a state pill that the main window feeds with ``Status`` objects.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import gamefacts
from ..core.backends import Status
from ..core.directkinds import (
    DIRECT_KINDS,
    DIRECT_LABEL,
    LAUNCHER_KINDS,
    LAUNCHER_LABEL,
    RETIRED_KINDS,
)
from ..core.gamefacts import InstallFacts, display_state
from ..core.profilenames import profile_display
from .game_order import game_sort_key, is_installed
from .images import ImageLoader
from .theme import ACCENT, badge_style, cover_pixmap, scaled

CARD_SCALE = 0.75
NEW_DAYS = 14
RECENT_MAX = 4
TILE_WIDTH = scaled(244)
TILE_ART_H = round(TILE_WIDTH * 9 / 16)
TILE_HEIGHT = TILE_ART_H
# Every library card is a Continue playing tile: wide art with the name on it (user 11-21).
CARD_WIDTH = TILE_WIDTH
CARD_ART_H = TILE_ART_H
CARD_HEIGHT = TILE_HEIGHT


def card_width_for(available: int) -> int:
    """Share rows within the scaled card limits, growing with the viewport."""
    gap = scaled(14)
    minimum, maximum = scaled(130 * CARD_SCALE), scaled(211 * CARD_SCALE)
    preferred = max(minimum, min(maximum, round(available * 132 * CARD_SCALE / 1016)))
    per_row = max(1, (available + gap) // (preferred + gap))
    return max(minimum, min(maximum, (available - (per_row - 1) * gap) // per_row))


def tile_width_for(available: int) -> int:
    """The width of every library tile: up to four share a row, two narrow cards wide at most."""
    gap = scaled(14)
    columns = min(RECENT_MAX, max(1, (available + gap) // (scaled(180) + gap)))
    return max(1, min(2 * card_width_for(available), (available - (columns - 1) * gap) // columns))


def _tile_text(width: int, height: int) -> tuple[QRect, QRect, QRect]:
    """The name, top badge and quick play rectangles of a tile, shared by every tile kind."""
    return (
        QRect(scaled(12), height - scaled(26), width - scaled(24), scaled(20)),
        QRect(scaled(12), scaled(10), width - scaled(60), scaled(20)),
        QRect(width - scaled(40), scaled(8), scaled(32), scaled(32)),
    )


def _place_badge(badge: QLabel, rect: QRect) -> None:
    """Fit a tile's state pill to its text, at the top left of ``rect``."""
    from .theme import repolish

    repolish(badge)
    badge.ensurePolished()
    width = min(badge.sizeHint().width(), rect.width())
    badge.setGeometry(rect.x(), rect.y(), width, rect.height())


def _paint_scrim(painter: QPainter, path, height: int) -> None:
    from PyQt6.QtGui import QLinearGradient

    scrim = QLinearGradient(0, height * 0.35, 0, height)
    scrim.setColorAt(0.0, QColor(6, 8, 13, 0))
    scrim.setColorAt(1.0, QColor(6, 8, 13, 210))
    painter.fillPath(path, scrim)

#: Focus is drawn on the card itself so keyboard users can see where Tab is.
_FOCUS_RING = (
    f"QFrame#GameCard:focus, QFrame#AddGameCard:focus {{"
    f" border: 2px solid {ACCENT}; }}"
)


def profile_title(name: str, profile: dict[str, Any]) -> str:
    """The display title for a profile (falls back to the profile name)."""
    return str(profile.get("title") or name)


def display_title(name: str, profiles: dict[str, dict[str, Any]]) -> str:
    """Like :func:`profile_title`, plus the profile id when another profile shares it."""
    return profile_display(name, profiles.get(name) or {}, profiles)


#: Friendly descriptions for the internal backend kinds shown on cards and pages.
_BACKEND_LABELS = {
    "stove-czn": "Windows game via Proton",
    # Launcher-free from the Yostar manifest by default, as the Store says (#1413, #1450).
    "arknights-yostar": DIRECT_LABEL,
    "stellasora-yostar": DIRECT_LABEL,
    **dict.fromkeys(DIRECT_KINDS, DIRECT_LABEL),
    **dict.fromkeys(LAUNCHER_KINDS, LAUNCHER_LABEL),
    "fgo-lepton": "Android container",
    **dict.fromkeys(RETIRED_KINDS, "Old launcher (retired)"),
    "wine": "Wine / Proton game",
    "steam": "Steam game",
    "script": "Custom script",
}


def backend_label(kind: str) -> str:
    """Plain-language text for a backend kind (raw ids belong in logs)."""
    key = (kind or "").strip().lower()
    return _BACKEND_LABELS.get(key, (key or "game").replace("-", " ").title())


def count_text(count: int, singular: str, plural: str | None = None) -> str:
    """``1 runner`` / ``3 runners`` instead of the ugly ``runner(s)``."""
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


def profile_art_candidates(profile: dict[str, Any]) -> list[str]:
    """Artwork paths to try, in priority order. Never downloads anything.

    Ordering is by provenance: any user image beats an official one, which
    beats third-party scrapes (the old bug was a third-party profile cover
    hiding the publisher's official hero).  A cached publisher image is the
    last resort when nothing is recorded on the profile.
    """
    keys = ("custom_background", "cover", "hero", "thumb", "artwork")
    try:
        from ..core import artwork

        out = artwork.ordered_paths(profile, keys)
        base = artwork.slug(profile, profile.get("title"))
        for kind in ("cover", "hero", "cover-wide", "tile"):
            cached = artwork.cached(base, kind)
            if cached is not None and str(cached) not in out:
                out.append(str(cached))
        return out
    except Exception:  # noqa: BLE001 - absent cache/import is not fatal
        return []


def _parse_stamp(value: Any) -> datetime | None:
    """The moment an ISO-8601 profile stamp names, or None when it is empty or unreadable."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _ago(value: Any) -> str:
    """``2 hours ago`` from a profile stamp; empty when the stamp is missing or unreadable.

    A hand-edited config can carry anything here, so this never raises: the strips simply drop
    the line instead of taking the library down.
    """
    stamp = _parse_stamp(value)
    if stamp is None:
        return ""
    seconds = max(0, int((datetime.now(UTC) - stamp).total_seconds()))
    if seconds < 60:
        return "just now"
    for unit, size, until in (
        ("minute", 60, 3600),
        ("hour", 3600, 86400),
        ("day", 86400, 7 * 86400),
        ("week", 7 * 86400, 30 * 86400),
        ("month", 30 * 86400, 365 * 86400),
    ):
        if seconds < until:
            count = max(1, seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "over a year ago"


def _is_new(value: Any) -> bool:
    """Whether a stamp is inside the NEW window."""
    stamp = _parse_stamp(value)
    if stamp is None:
        return False
    return timedelta(0) <= datetime.now(UTC) - stamp < timedelta(days=NEW_DAYS)


def recent_art_candidates(profile: dict[str, Any]) -> list[str]:
    """Use recorded wide art and the store's cached cover-wide slot, without fetching."""
    from ..core import artwork

    paths = artwork.ordered_paths(profile, ("custom_background", "wide", "hero"))
    cached = artwork.cached(artwork.slug(profile, profile.get("title")), "cover-wide")
    if cached is not None:
        paths.append(str(cached))
    return list(dict.fromkeys(paths + profile_art_candidates(profile)))


def _drop_shadow(widget: QWidget, blur: int, alpha: int) -> QGraphicsDropShadowEffect:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(scaled(blur))
    effect.setOffset(0, scaled(6))
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)
    return effect


class FlowLayout(QLayout):
    """A wrapping layout, adapted from the Qt Flow Layout example."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 16) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt naming
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        # Split into lines first, then place each line with its items centred vertically
        # (a pill and a plain label of different heights stay on one baseline).
        lines: list[list[tuple[QLayoutItem, QSize]]] = [[]]
        x = effective.x()
        for item in self._items:
            if item.isEmpty():
                continue  # a hidden widget takes no place (and no spacing)
            hint = item.sizeHint()
            if lines[-1] and x + hint.width() > effective.right() + 1:
                lines.append([])
                x = effective.x()
            lines[-1].append((item, hint))
            x += hint.width() + self._spacing
        y = effective.y()
        for number, line in enumerate(lines):
            if not line:
                continue
            if number:
                y += self._spacing
            height = max(hint.height() for _item, hint in line)
            if apply:
                x = effective.x()
                for item, hint in line:
                    top = y + (height - hint.height()) // 2
                    item.setGeometry(QRect(QPoint(x, top), hint))
                    x += hint.width() + self._spacing
            y += height
        return y - rect.y() + margins.bottom()


class TileLayout(FlowLayout):
    """Lay out same-sized 16:9 tiles, as many to a row as fit."""

    def __init__(self, parent=None):
        super().__init__(parent, spacing=scaled(14))

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        # The tiles are resized to the row, so their current width is no minimum: a wide window's
        # tiles must not hold the page wide after it shrinks.
        margins = self.contentsMargins()
        return QSize(scaled(130 * CARD_SCALE) + margins.left() + margins.right(), 0)

    def _do_layout(self, rect: QRect, *, apply: bool) -> int:
        items = [item for item in self._items if not item.isEmpty()]
        if not items:
            return 0
        gap = scaled(14)
        width = tile_width_for(rect.width())
        columns = max(1, (rect.width() + gap) // (width + gap))
        height = round(width * 9 / 16)
        for index, item in enumerate(items):
            if apply:
                widget = item.widget()
                if widget.width() != width:
                    widget.resize_tile(width)
                item.setGeometry(
                    QRect(
                        rect.x() + (index % columns) * (width + gap),
                        rect.y() + (index // columns) * (height + scaled(16)),
                        width,
                        height,
                    )
                )
        rows = (len(items) + columns - 1) // columns
        return rows * height + (rows - 1) * scaled(16)


class CoverLabel(QLabel):
    """A cover with an accent edge confined to its rounded artwork."""

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtGui import QPainterPath, QPen

        super().paintEvent(event)
        card = self.parentWidget()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = scaled(16)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), radius, radius)
        # The scrim under the name, as on a Continue playing tile.
        _paint_scrim(painter, path, self.height())
        if self.property("running") or card.hasFocus():
            painter.setPen(QPen(QColor(ACCENT), scaled(2)))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), radius, radius)
        else:
            hover = getattr(card, "_hover", False)
            painter.setPen(QColor(150, 180, 255, 90) if hover else QColor(150, 180, 255, 40))
            painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), radius, radius)


class GameCard(QFrame):
    """A wide cover with the name on it, a state badge and hover quick play (a tile)."""

    clicked = pyqtSignal(str)
    play_clicked = pyqtSignal(str)
    resized = pyqtSignal()

    def __init__(
        self,
        profile: str,
        title: str,
        backend: str = "",
        art: Any = None,
        fallback_seed: str | None = None,
        parent: QWidget | None = None,
        *,
        added_at: Any = None,
        last_played: Any = None,
        favourite: bool = False,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self._title = str(title)
        self._backend = backend_label(backend)
        self._ready = False
        self._has_state_line = False
        self._hover = False
        self._focused = False
        self._source = art
        self._seed = fallback_seed or profile
        self._favourite = favourite
        self.setObjectName("GameCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(self._title)
        self.setToolTip(self._title)
        self.art = CoverLabel(self)
        self.art.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.art.setStyleSheet("QLabel[running=true] { background: transparent; }")
        self._shadow = _drop_shadow(self.art, 18, 100)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("TileTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.meta_label = QLabel(self)
        self.meta_label.setObjectName("LibraryMeta")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._default_meta = f"added {_ago(added_at)}" if _is_new(added_at) else self._backend
        self.meta_label.setText(self._default_meta)
        self.meta_label.setToolTip(self._backend)
        # The tile shows only the name; what the meta line said moves to the tooltip.
        self.meta_label.hide()
        meta = self._default_meta
        self.setToolTip(f"{self._title}\n{meta}" if meta else self._title)
        # Keep the public badge for callers which inspect the current display state.
        self.badge = QLabel(self)
        self.badge.setObjectName("TileBadge")
        self.badge.setTextFormat(Qt.TextFormat.PlainText)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.hide()
        self.new_pill = QLabel("NEW", self)
        self.new_pill.setObjectName("NewPill")
        self.new_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.new_pill.setVisible(_is_new(added_at) and not last_played and not favourite)
        self.play_button = QPushButton("▶", self)
        self.play_button.setObjectName("QuickPlay")
        self.play_button.setAccessibleName(f"Play {title}")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(lambda: self.play_clicked.emit(self.profile))
        self.play_button.hide()
        self.play_button.installEventFilter(self)
        self.resize_tile(CARD_WIDTH)

    def resize_tile(self, width: int) -> None:
        height = round(width * 9 / 16)
        self.setFixedSize(width, height)
        self.art.setGeometry(0, 0, width, height)
        self.art.setPixmap(cover_pixmap(self._source, width, height, self._seed, radius=scaled(16)))
        name, badge, play = _tile_text(width, height)
        if self._favourite:
            name.setLeft(name.left() + scaled(16))
        self.title_label.setGeometry(name)
        self.title_label.ensurePolished()
        self.title_label.setText(
            QFontMetrics(self.title_label.font()).elidedText(
                self._title, Qt.TextElideMode.ElideRight, self.title_label.width()
            )
        )
        self.meta_label.setGeometry(badge)
        self._badge_rect = badge
        _place_badge(self.badge, badge)
        self.badge.setVisible(self._has_state_line)
        if self._favourite:
            if not hasattr(self, "heart"):
                self.heart = QLabel("♥", self)
                self.heart.setStyleSheet("color: #ff7aa2; background: transparent;")
                self.heart.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.heart.setGeometry(scaled(12), name.top(), scaled(13), name.height())
        self.new_pill.setGeometry(scaled(12), scaled(10), scaled(44), scaled(18))
        self.play_button.setGeometry(play)
        self.resized.emit()

    def set_art(self, pixmap: Any) -> None:
        self._source = pixmap
        self.art.setPixmap(
            cover_pixmap(pixmap, self.art.width(), self.art.height(), self._seed, radius=scaled(13))
        )

    def set_state(self, state: str | Status, running: bool = False) -> None:
        status = state if isinstance(state, Status) else None
        if status is not None:
            state = display_state(status)
            running = gamefacts.is_running(status)
            ready = (
                InstallFacts.from_status(status).state() == "complete"
                and gamefacts.primary_operation(status) == "launch"
            )
        else:
            ready = state == "installed" and not running
        pill_state, label = badge_style(state)
        if state == "installed" and gamefacts.update_available(status):
            pill_state, label = "info", "Update available"
        self.badge.setText(label)
        self.badge.setProperty("state", pill_state)
        _place_badge(self.badge, self._badge_rect)
        self._has_state_line = not ready and state != "unknown"
        self.badge.setVisible(self._has_state_line)
        if self._has_state_line:
            self.new_pill.hide()
        self.meta_label.setText(self._default_meta if ready or state == "unknown" else label)
        self.setProperty("running", bool(running))
        self.art.setProperty("running", bool(running))
        self.art.update()
        self._ready = ready
        self._update_play()

    def _update_play(self) -> None:
        self.play_button.setVisible(
            self._ready and (self._hover or self._focused or self.play_button.hasFocus())
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self._update_play()
        self.art.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._update_play()
        self.art.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._focused = True
        self._update_play()
        self.art.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._focused = False
        if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            # Qt assigns the next focus after this event. Keep the button alive for that move.
            QTimer.singleShot(0, self._update_play)
        else:
            self._update_play()
        self.art.update()
        super().focusOutEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.play_button and event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._update_play)
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit(self.profile)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self.profile)
        super().mouseReleaseEvent(event)

class AddGameCard(QFrame):
    """The dashed ``+ Add game`` card at the end of the grid."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AddGameCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Add game")
        self.setStyleSheet(_FOCUS_RING)
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self.setToolTip("Add a game to your library")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(scaled(10) for _ in range(4)))
        layout.setSpacing(scaled(6))
        layout.addStretch(1)

        plus_frame = QFrame()
        plus_frame.setObjectName("AddPlus")
        plus_frame.setFixedSize(scaled(40), scaled(40))
        plus_layout = QVBoxLayout(plus_frame)
        plus_layout.setContentsMargins(0, 0, 0, 0)
        plus = QLabel("+")
        plus.setObjectName("AddPlusGlyph")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus_layout.addWidget(plus)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(plus_frame)
        row.addStretch(1)
        layout.addLayout(row)

        label = QLabel("Add game")
        label.setObjectName("CardTitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        hint = QLabel("Add a game to install it")
        hint.setObjectName("CardMeta")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def resize_tile(self, width: int) -> None:
        self.setFixedSize(width, round(width * 9 / 16))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class RecentTile(QFrame):
    """One wide "continue playing" tile: the cover, a scrim and the name on it.

    The art is painted by the tile itself so the scrim and the rounded corners move with a
    resize; a child label carries the name so it elides like every other label in the app.
    """

    open_requested = pyqtSignal(str)  # profile name
    play_clicked = pyqtSignal(str)

    def __init__(self, profile: str, title: str, when: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setObjectName("RecentTile")
        self.setFixedSize(TILE_WIDTH, TILE_ART_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Continue {title}")
        self.setToolTip(f"{title}\n{when}" if when else str(title))
        self._pixmap = QPixmap()
        self._hover = False
        self._focused = False
        self._ready = False
        self._shadow = _drop_shadow(self, 22, 90)

        self.name = QLabel(self)
        self.name.setObjectName("TileTitle")
        self._title = str(title)
        self.name.setTextFormat(Qt.TextFormat.PlainText)
        self.name.setText(title)
        self.name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge = QLabel(self)
        self.badge.setObjectName("TileBadge")
        self.badge.setTextFormat(Qt.TextFormat.PlainText)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.hide()
        self.play_button = QPushButton("▶", self)
        self.play_button.setObjectName("QuickPlay")
        self.play_button.setAccessibleName(f"Play {title}")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(lambda: self.play_clicked.emit(self.profile))
        self.play_button.installEventFilter(self)
        self.play_button.hide()
        self.resize_tile(TILE_WIDTH)

    def resize_tile(self, width: int) -> None:
        self.setFixedSize(width, round(width * 9 / 16))
        name, badge, play = _tile_text(width, self.height())
        self.name.setGeometry(name)
        self.name.ensurePolished()
        self.name.setText(
            QFontMetrics(self.name.font()).elidedText(
                self._title, Qt.TextElideMode.ElideRight, self.name.width()
            )
        )
        self._badge_rect = badge
        _place_badge(self.badge, badge)
        self.play_button.setGeometry(play)

    def set_state(self, state: str | Status, running: bool = False) -> None:
        status = state if isinstance(state, Status) else None
        if status is not None:
            state = display_state(status)
            running = gamefacts.is_running(status)
            ready = (
                InstallFacts.from_status(status).state() == "complete"
                and gamefacts.primary_operation(status) == "launch"
            )
        else:
            ready = state == "installed" and not running
        pill_state, label = badge_style(state)
        if state == "installed" and gamefacts.update_available(status):
            pill_state, label = "info", "Update available"
        self.badge.setText(label)
        self.badge.setProperty("state", pill_state)
        _place_badge(self.badge, self._badge_rect)
        self.badge.setVisible(not ready and state != "unknown")
        self.setProperty("running", bool(running))
        self._ready = ready
        self._update_play()
        self.update()

    # Match the portrait card's hover and keyboard behaviour.
    def _update_play(self) -> None:
        self.play_button.setVisible(
            self._ready and (self._hover or self._focused or self.play_button.hasFocus())
        )

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.play_button and event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._update_play)
        return super().eventFilter(watched, event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._focused = True
        self._update_play()
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._focused = False
        if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            QTimer.singleShot(0, self._update_play)
        else:
            self._update_play()
        self.update()
        super().focusOutEvent(event)

    def set_art(self, pixmap: Any) -> None:
        if isinstance(pixmap, QPixmap):
            self._pixmap = pixmap
        elif pixmap is not None and hasattr(pixmap, "isNull"):
            self._pixmap = QPixmap.fromImage(pixmap)
        else:
            self._pixmap = QPixmap()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        from PyQt6.QtGui import QPainterPath

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = scaled(16)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), radius, radius)
        painter.setClipPath(path)
        if self._pixmap.isNull():
            painter.fillPath(path, QColor("#1b2436"))
        else:
            painter.drawPixmap(
                0,
                0,
                cover_pixmap(
                    self._pixmap, self.width(), self.height(), self.profile, radius=scaled(16)
                ),
            )
        _paint_scrim(painter, path, self.height())
        painter.setClipping(False)
        edge = (
            QColor(ACCENT)
            if self.hasFocus() or self.property("running")
            else (QColor(150, 180, 255, 90) if self._hover else QColor(150, 180, 255, 40))
        )
        painter.setPen(edge)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), radius, radius)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hover = True
        self._update_play()
        self._shadow.setBlurRadius(scaled(30))
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hover = False
        self._update_play()
        self._shadow.setBlurRadius(scaled(22))
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open_requested.emit(self.profile)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.open_requested.emit(self.profile)
        super().mouseReleaseEvent(event)


class LibraryPage(QWidget):
    """Searchable portrait grid with recent and favourite game strips."""

    open_game = pyqtSignal(str)
    play_game = pyqtSignal(str)
    add_game = pyqtSignal()
    remove_game = pyqtSignal(str)
    favourite_toggled = pyqtSignal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._cards: dict[str, GameCard] = {}
        #: The profiles and statuses the grid was last shown with, kept for sorting.
        self._profiles: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, Status] = {}
        #: Badge states of games with an install, update or repair under way ("installing",
        #: "waiting"...). The status probe says "not installed" until an install ends, so the
        #: running operation outranks it on the cards (user sim r2 item 5).
        self._busy: dict[str, str] = {}
        #: What the cards were last built from (see :meth:`_signature`), to skip a no-op rebuild.
        self._shown: tuple | None = None
        # Cover art is decoded off the GUI thread; cards show a placeholder first.
        self._images = ImageLoader(parent=self)
        #: Cards whose size changed; their covers are decoded again once resizing settles.
        self._art_dirty: dict[GameCard, tuple[str, str, dict[str, Any]]] = {}
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(150)
        self._art_timer.timeout.connect(self._redecode_resized_art)

        self._favourite_cards: dict[str, GameCard] = {}
        self.recent_tiles: list[RecentTile] = []
        self._live: set[QWidget] = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(scaled(28), scaled(22), scaled(28), 0)
        root.setSpacing(0)
        body = QWidget()
        body.setObjectName("LibraryBody")
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 0, 0, scaled(40))
        box.setSpacing(scaled(22))
        header = QHBoxLayout()
        header.setSpacing(scaled(12))
        self.search = QLineEdit()
        self.search.setObjectName("LibrarySearch")
        self.search.setPlaceholderText("Search your games")
        self.search.setAccessibleName("Search your games")
        self.search.setFixedHeight(scaled(44))
        self.search.installEventFilter(self)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search, 1)
        self.add_button = QPushButton("+ Add a game")
        self.add_button.setObjectName("LibraryAdd")
        self.add_button.setFixedHeight(scaled(44))
        self.add_button.clicked.connect(self.add_game.emit)
        header.addWidget(self.add_button)
        box.addLayout(header)
        self.recent_section, self._recent_holder, self._recent_flow = self._section(
            "CONTINUE PLAYING"
        )
        self.favourites_section, self._fav_holder, self._fav_flow = self._section("FAVOURITES")
        # One "LIBRARY" grid until a status is known, then INSTALLED and NOT INSTALLED (#1621).
        self.grid_section, self._container, self._flow = self._section("LIBRARY")
        self.grid_title = self.grid_section.title_label
        self.not_installed_section, self._rest_container, self._rest_flow = self._section(
            "NOT INSTALLED"
        )
        self.not_installed_section.hide()
        #: The installed games the grid was split by, or ``None`` while no status is known.
        self._split_shown: frozenset[str] | None = None
        box.addWidget(self.recent_section)
        box.addWidget(self.favourites_section)
        box.addWidget(self.grid_section)
        box.addWidget(self.not_installed_section)
        self.no_matches = QLabel("No game matches")
        self.no_matches.setObjectName("LibraryMeta")
        self.no_matches.hide()
        box.addWidget(self.no_matches)
        self.empty_state = QWidget()
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(0, scaled(60), 0, scaled(60))
        add = QPushButton("Add a game")
        self.empty_add = add
        self.subtitle = QLabel(
            f"No games yet. Click {add.text()} to open the Store and install your first one."
        )
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.subtitle)
        add.setObjectName("LibraryAdd")
        add.setFixedHeight(scaled(44))
        add.clicked.connect(self.add_game.emit)
        empty.addWidget(add, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(self.empty_state)
        box.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("LibraryScroll")
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll)

    @staticmethod
    def _section(title: str):
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(scaled(10))
        row = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("LibrarySection")
        row.addWidget(label)
        row.addStretch(1)
        layout.addLayout(row)
        holder = QWidget()
        flow = TileLayout(holder)
        layout.addWidget(holder)
        section.title_label = label
        return section, holder, flow

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            watched is self.search
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.search.clear()
            return True
        return super().eventFilter(watched, event)

    def _filter(self, text: str = "") -> None:
        query = text.strip().casefold()
        for name, card in self._cards.items():
            card.setVisible(
                not query or query in name.casefold() or query in card._title.casefold()
            )

        def any_shown(container: QWidget) -> bool:
            return any(c.parent() is container and not c.isHidden() for c in self._cards.values())

        split = self._split_shown is not None
        self.grid_section.setVisible(not split or any_shown(self._container))
        self.not_installed_section.setVisible(split and any_shown(self._rest_container))
        self.recent_section.setVisible(bool(self.recent_tiles) and not query)
        self.favourites_section.setVisible(bool(self._favourite_cards) and not query)
        self.no_matches.setVisible(
            bool(query) and not any(not c.isHidden() for c in self._cards.values())
        )
        self._flow.invalidate()
        self._rest_flow.invalidate()

    def card_menu(self, profile: str) -> QMenu:
        """The right-click menu of a game card."""
        menu = QMenu(self)
        card = self._cards.get(profile)
        if card is not None and card._ready:
            menu.addAction("Play").triggered.connect(lambda: self.play_game.emit(profile))
        menu.addAction("Open").triggered.connect(lambda: self.open_game.emit(profile))
        menu.addAction("Uninstall or remove…").triggered.connect(
            lambda: self.remove_game.emit(profile)
        )
        pinned = bool(self._profiles.get(profile, {}).get("favourite"))
        menu.addAction(
            "Remove from favourites" if pinned else "Add to favourites"
        ).triggered.connect(lambda: self.favourite_toggled.emit(profile, not pinned))
        return menu

    def _show_card_menu(self, profile: str, card: QWidget, pos: Any) -> None:
        """Show a card's menu, then let Qt delete it (one QMenu per right-click leaked; b175)."""
        menu = self.card_menu(profile)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(card.mapToGlobal(pos))

    # ---- population --------------------------------------------------------
    def refresh(self, profiles: dict[str, dict[str, Any]]) -> None:
        """Rebuild the grid from the config's profiles.

        A keyboard user's place survives the rebuild: the card that had focus gets it back (#942).
        """
        self._profiles = profiles
        # A removed profile's status must not survive into a later profile with the same name.
        self._statuses = {
            name: status for name, status in self._statuses.items() if name in profiles
        }
        self._busy = {name: busy for name, busy in self._busy.items() if name in profiles}
        shown = self._signature(profiles)
        if shown == self._shown:
            return  # nothing a card shows changed: keep the cards and their decoded covers (#624)
        # Remembered only once the rebuild finished: a failed one must run again (#1207 b176).
        self._shown = None
        focused = QApplication.focusWidget()
        focused_card = next(
            (c for c in self._live if c is focused or (focused and c.isAncestorOf(focused))), None
        )
        focused_name = focused_card.profile if focused_card is not None else None
        focused_recent = isinstance(focused_card, RecentTile)
        focused_favourite = focused_card in self._favourite_cards.values()
        self._clear()
        split = self._split(profiles)
        for name in self._ordered(profiles):
            profile = profiles[name]
            title = display_title(name, profiles)
            installed = split is None or name in split
            card = GameCard(
                name,
                title,
                str(profile.get("backend") or ""),
                None,
                fallback_seed=title,
                parent=self._container if installed else self._rest_container,
                added_at=profile.get("added_at"),
                last_played=profile.get("last_played"),
            )
            card.clicked.connect(self.open_game.emit)
            card.play_clicked.connect(self.play_game.emit)
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, n=name, c=card: self._show_card_menu(n, c, pos)
            )
            shown_state = self._shown_state(name)
            if shown_state is not None:
                card.set_state(shown_state)
            (self._flow if installed else self._rest_flow).addWidget(card)
            self._cards[name] = card
            self._live.add(card)
            self._request_art(name, card, title, recent_art_candidates(profile))
            card.resized.connect(
                lambda n=name, c=card, t=title, p=profile: self._art_resized(n, c, t, p)
            )

        self._build_strips(profiles)
        self._split_shown = split
        self.grid_title.setText("LIBRARY" if split is None else "INSTALLED")
        self.empty_state.setVisible(not self._cards)
        self._filter(self.search.text())
        # Not adjustSize(): while the page is hidden (a removal runs from the game
        # page) that shrinks the container to one card wide, and nothing re-flows it.
        self._flow.invalidate()
        self._rest_flow.invalidate()
        self._shown = shown
        if focused_recent:
            target = next((t for t in self.recent_tiles if t.profile == focused_name), None)
        elif focused_favourite:
            target = self._favourite_cards.get(focused_name)
        else:
            target = self._cards.get(focused_name)
        if target is not None:
            target.setFocus()

    def _ordered(self, profiles: dict[str, dict[str, Any]]) -> list[str]:
        """Game (profile) keys in the sidebar's order, so both views list games the same way.

        A running game stays with the installed ones: starting a game from its card does not
        move the card (or rebuild the grid)."""

        def key(name: str) -> tuple[int, str, str]:
            status = self._statuses.get(name)
            title = display_title(name, profiles)
            return game_sort_key(name, title, status, running_first=False)

        return sorted(profiles, key=key)

    def _split(self, profiles: dict[str, dict[str, Any]]) -> frozenset[str] | None:
        """The installed games, or ``None`` while no game's status is known (#1621)."""
        if not any(name in self._statuses for name in profiles):
            return None
        return frozenset(name for name in profiles if is_installed(self._statuses.get(name)))

    def _signature(self, profiles: dict[str, dict[str, Any]]) -> tuple:
        """Everything a card shows: order, section, title, kind and each art file with its mtime."""
        rows = []
        for name in self._ordered(profiles):
            profile = profiles[name]
            art = []
            candidates = profile_art_candidates(profile) + recent_art_candidates(profile)
            for path in dict.fromkeys(candidates):
                try:
                    stamp = os.stat(path).st_mtime_ns
                except OSError:
                    stamp = None
                art.append((path, stamp))
            rows.append(
                (
                    name,
                    display_title(name, profiles),
                    str(profile.get("backend") or ""),
                    tuple(art),
                    bool(profile.get("favourite")),
                    str(profile.get("last_played") or ""),
                    str(profile.get("added_at") or ""),
                )
            )
        return (tuple(rows), self._split(profiles))

    def _art_resized(self, name: str, card: GameCard, title: str, profile: dict[str, Any]) -> None:
        """Note a resized card; its cover is decoded at the new size once resizing stops."""
        self._art_dirty[card] = (name, title, profile)
        self._art_timer.start()

    def _redecode_resized_art(self) -> None:
        dirty, self._art_dirty = self._art_dirty, {}
        for card, (name, title, profile) in dirty.items():
            if card in self._live:
                self._request_art(name, card, title, recent_art_candidates(profile))

    def _request_art(self, name: str, card: GameCard, title: str, candidates: list[str]) -> None:
        """Decode one cover in the background, then update its card if still shown."""
        size = card.art.size()

        def apply(pixmap) -> None:
            if card not in self._live or card.art.size() != size:
                return
            card.set_art(pixmap)

        width, height = size.width(), size.height()
        self._images.request(candidates, width, height, apply, cover=True)

    def _build_strips(self, profiles) -> None:
        played = [(name, p) for name, p in profiles.items() if _parse_stamp(p.get("last_played"))]
        played.sort(key=lambda row: _parse_stamp(row[1]["last_played"]), reverse=True)
        for name, profile in played[:RECENT_MAX]:
            tile = RecentTile(
                name,
                display_title(name, profiles),
                _ago(profile["last_played"]),
                self._recent_holder,
            )
            tile.open_requested.connect(self.open_game.emit)
            tile.play_clicked.connect(self.play_game.emit)
            if (shown_state := self._shown_state(name)) is not None:
                tile.set_state(shown_state)
            self.recent_tiles.append(tile)
            self._live.add(tile)
            self._recent_flow.addWidget(tile)

            def apply(pixmap, tile=tile):
                if tile in self._live:
                    tile.set_art(pixmap)

            self._images.request(
                recent_art_candidates(profile), scaled(600), scaled(338), apply, cover=True
            )
        for name in self._ordered(profiles):
            profile = profiles[name]
            if not profile.get("favourite"):
                continue
            title = display_title(name, profiles)
            card = GameCard(
                name,
                title,
                str(profile.get("backend") or ""),
                parent=self._fav_holder,
                favourite=True,
                fallback_seed=title,
                added_at=profile.get("added_at"),
                last_played=profile.get("last_played"),
            )
            card.clicked.connect(self.open_game.emit)
            card.play_clicked.connect(self.play_game.emit)
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, n=name, c=card: self._show_card_menu(n, c, pos)
            )
            if (shown_state := self._shown_state(name)) is not None:
                card.set_state(shown_state)
            self._fav_flow.addWidget(card)
            self._favourite_cards[name] = card
            self._live.add(card)
            self._request_art(name, card, title, recent_art_candidates(profile))
            card.resized.connect(
                lambda n=name, c=card, t=title, p=profile: self._art_resized(n, c, t, p)
            )

    def wait_for_images(self, timeout_ms: int = 5000) -> None:
        """Wait for pending cover decodes (tests and shutdown helpers)."""
        self._images.wait(timeout_ms)

    def _clear(self) -> None:
        self._cards.clear()
        self._favourite_cards.clear()
        self.recent_tiles = []
        self._live.clear()
        for flow in (self._flow, self._rest_flow, self._recent_flow, self._fav_flow):
            while flow.count():
                item = flow.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()

    # ---- state -------------------------------------------------------------
    def set_state(self, profile: str, state: str | Status, running: bool = False) -> None:
        if isinstance(state, Status) and profile in self._profiles:
            self._statuses[profile] = state
            if (
                self._ordered(self._profiles) != list(self._cards)
                or self._split(self._profiles) != self._split_shown
            ):
                # The status changed the game's group or section: rebuild in the sidebar's
                # order. The rebuild applies every stored status, including this one.
                self.refresh(self._profiles)
                return
        busy = self._busy.get(profile)
        if busy is not None and not running and not (
            isinstance(state, Status) and gamefacts.is_running(state)
        ):
            state = busy
        for card in (self._cards.get(profile), self._favourite_cards.get(profile)):
            if card is not None:
                card.set_state(state, running)

        for tile in self.recent_tiles:
            if tile.profile == profile:
                tile.set_state(state, running)

    def _shown_state(self, name: str) -> str | Status | None:
        """What a card for ``name`` shows: a running operation, else the last status."""
        busy = self._busy.get(name)
        return busy if busy is not None else self._statuses.get(name)

    def set_busy(self, profile: str, busy: str | None) -> None:
        """Show ``busy`` (a badge state such as "installing") on the game's cards, or
        go back to its last status when ``busy`` is None."""
        if busy:
            self._busy[profile] = busy
        elif self._busy.pop(profile, None) is None:
            return
        shown = self._shown_state(profile)
        shown = "unknown" if shown is None else shown
        for card in (self._cards.get(profile), self._favourite_cards.get(profile)):
            if card is not None:
                card.set_state(shown)
        for tile in self.recent_tiles:
            if tile.profile == profile:
                tile.set_state(shown)

    def clear_states(self) -> None:
        for card in list(self._cards.values()) + list(self._favourite_cards.values()):
            card.set_state("unknown", False)
        for tile in self.recent_tiles:
            tile.set_state("unknown", False)


__all__ = [
    "RecentTile",
    "CARD_WIDTH",
    "CARD_ART_H",
    "CARD_HEIGHT",
    "FlowLayout",
    "GameCard",
    "AddGameCard",
    "LibraryPage",
    "backend_label",
    "count_text",
    "display_title",
    "profile_title",
    "profile_art_candidates",
]
