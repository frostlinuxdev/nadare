# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The library's card widgets and the private helpers they share.

Split out of :mod:`nadare.gui.library` (#1601); the library imports these back, so
``nadare.gui.library.GameCard`` and friends stay the same objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
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
        # Bottom right, inset the same scaled 8 px from both edges as every tile's corners.
        QRect(width - scaled(40), height - scaled(40), scaled(32), scaled(32)),
    )


def _name_clear_of_play(name: QRect, play: QRect) -> QRect:
    """``name`` ended before the quick play button, with a small gap so no text sits under it."""
    return QRect(
        name.x(),
        name.y(),
        max(scaled(24), play.left() - scaled(8) - name.x()),
        name.height(),
    )


def _elide_name(label: QLabel, title: str, rect: QRect) -> None:
    """Place a tile's name on ``rect`` and elide it to that width."""
    label.setGeometry(rect)
    label.ensurePolished()
    label.setText(
        QFontMetrics(label.font()).elidedText(title, Qt.TextElideMode.ElideRight, rect.width())
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


#: Friendly descriptions for the internal backend kinds shown on cards and pages.
_BACKEND_LABELS = {
    "stove-czn": "Windows game via Proton",
    # Launcher-free from the Yostar manifest by default, as the Store says (#1413, #1450).
    "arknights-yostar": DIRECT_LABEL,
    "stellasora-yostar": DIRECT_LABEL,
    "hbr-yostar": DIRECT_LABEL,
    "azurpromilia-jp-yostar": DIRECT_LABEL,
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
        self._name_rect = name
        self.play_button.setGeometry(play)
        self._place_title()
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
        self.resized.emit()

    def _place_title(self) -> None:
        """Lay out the name; while the Play button shows it ends before the button."""
        rect = self._name_rect
        if not self.play_button.isHidden():
            rect = _name_clear_of_play(rect, self.play_button.geometry())
        _elide_name(self.title_label, self._title, rect)

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
        self._place_title()

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
        self._name_rect = name
        self.play_button.setGeometry(play)
        self._place_name()
        self._badge_rect = badge
        _place_badge(self.badge, badge)

    def _place_name(self) -> None:
        """Lay out the name; while the Play button shows it ends before the button."""
        rect = self._name_rect
        if not self.play_button.isHidden():
            rect = _name_clear_of_play(rect, self.play_button.geometry())
        _elide_name(self.name, self._title, rect)

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
        self._place_name()

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
