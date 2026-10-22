# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The library: the grid of game cards and the permanent ``+ Add game`` card.

The library is presentation only. It renders whatever profiles the config holds
and shows a state pill that the main window feeds with ``Status`` objects.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .images import ImageLoader
from .theme import ACCENT, badge_style, cover_pixmap, repolish

CARD_WIDTH = 238
CARD_ART_H = 300
CARD_HEIGHT = CARD_ART_H + 86

#: Focus is drawn on the card itself so keyboard users can see where Tab is.
_FOCUS_RING = (
    f"QFrame#GameCard:focus, QFrame#AddGameCard:focus {{"
    f" border: 2px solid {ACCENT}; }}"
)


def profile_title(name: str, profile: dict[str, Any]) -> str:
    """The display title for a profile (falls back to the profile name)."""
    return str(profile.get("title") or name)


def display_title(name: str, profiles: dict[str, dict[str, Any]]) -> str:
    """Like :func:`profile_title`, plus the profile id when another profile shares it.

    Two profiles of the same game would otherwise be indistinguishable
    (``"Arknights · arknights-2"``); a unique title is shown unchanged.
    """
    title = profile_title(name, profiles.get(name) or {})
    folded = title.casefold()
    for other, data in profiles.items():
        if other != name and profile_title(other, data or {}).casefold() == folded:
            return f"{title} · {name}"
    return title


#: Friendly descriptions for the internal backend kinds shown on cards and pages.
_BACKEND_LABELS = {
    "stove-czn": "Windows game via Proton",
    "arknights-yostar": "Yostar launcher",
    "stellasora-yostar": "Yostar launcher",
    "fgo-lepton": "Android container",
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
        for kind in ("cover", "hero"):
            cached = artwork.cached(base, kind)
            if cached is not None and str(cached) not in out:
                out.append(str(cached))
        return out
    except Exception:  # noqa: BLE001 - absent cache/import is not fatal
        return []


def _drop_shadow(widget: QWidget, blur: int, alpha: int) -> QGraphicsDropShadowEffect:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 4)
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
        x, y, line_height = effective.x(), effective.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class GameCard(QFrame):
    """One game in the library grid: art, title, backend and a state pill."""

    clicked = pyqtSignal(str)  # profile name

    def __init__(
        self,
        profile: str,
        title: str,
        backend: str = "",
        art: Any = None,
        fallback_seed: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        seed = fallback_seed or profile
        self.setObjectName("GameCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(str(title))
        self.setStyleSheet(_FOCUS_RING)
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self.setToolTip(title)
        self._shadow = _drop_shadow(self, 18, 70)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 0)
        layout.setSpacing(0)

        self.art = QLabel()
        self.art.setFixedSize(CARD_WIDTH - 2, CARD_ART_H)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setPixmap(cover_pixmap(art, CARD_WIDTH - 2, CARD_ART_H, seed, radius=13))
        layout.addWidget(self.art)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(14, 11, 14, 13)
        footer_layout.setSpacing(7)

        metrics = QFontMetrics(self.font())
        self.title_label = QLabel(
            metrics.elidedText(str(title), Qt.TextElideMode.ElideRight, CARD_WIDTH - 30)
        )
        self.title_label.setObjectName("CardTitle")
        # A hand-edited profile title may contain '<'/'&': show it literally.
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setToolTip(Qt.convertFromPlainText(str(title)))
        footer_layout.addWidget(self.title_label)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(6)
        self.meta_label = QLabel(backend_label(backend))
        self.meta_label.setObjectName("CardMeta")
        self.meta_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        meta_row.addWidget(self.meta_label, 1)
        self.badge = QLabel("")
        self.badge.setObjectName("Pill")
        meta_row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignRight)
        footer_layout.addLayout(meta_row)
        layout.addWidget(footer)

    def set_state(self, state: str, running: bool = False) -> None:
        """Update the state pill from a backend state."""
        pill_state, label = badge_style(state)
        self.badge.setText(label)
        self.badge.setProperty("state", pill_state)
        repolish(self.badge)
        self.setProperty("running", bool(running))
        repolish(self)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._shadow.setBlurRadius(34)
        self._shadow.setColor(QColor(0, 0, 0, 120))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._shadow.setBlurRadius(18)
        self._shadow.setColor(QColor(0, 0, 0, 70))
        super().leaveEvent(event)

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
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        layout.addStretch(1)

        plus_frame = QFrame()
        plus_frame.setObjectName("AddPlus")
        plus_frame.setFixedSize(58, 58)
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


class LibraryPage(QWidget):
    """Scrollable grid of :class:`GameCard` widgets plus the add card."""

    open_game = pyqtSignal(str)
    add_game = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._cards: dict[str, GameCard] = {}
        # Cover art is decoded off the GUI thread; cards show a placeholder first.
        self._images = ImageLoader(parent=self)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(18)

        header = QVBoxLayout()
        header.setSpacing(6)
        kicker = QLabel("LIBRARY")
        kicker.setObjectName("Section")
        header.addWidget(kicker)
        title_row = QHBoxLayout()
        heading = QLabel("Your games")
        heading.setObjectName("Title")
        title_row.addWidget(heading)
        title_row.addStretch(1)
        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        title_row.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignBottom)
        header.addLayout(title_row)
        self.subtitle = QLabel("Choose a game to launch it, or add a new one.")
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setWordWrap(True)
        header.addWidget(self.subtitle)
        root.addLayout(header)

        self._container = QWidget()
        self._container.setObjectName("LibraryContainer")
        self._flow = FlowLayout(self._container, margin=2, spacing=18)
        self._container.setLayout(self._flow)

        scroll = QScrollArea()
        scroll.setObjectName("LibraryScroll")
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Tab goes to the cards, not the frame
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll, 1)

    # ---- population --------------------------------------------------------
    def refresh(self, profiles: dict[str, dict[str, Any]]) -> None:
        """Rebuild the grid from the config's profiles."""
        self._clear()
        for name, profile in profiles.items():
            title = display_title(name, profiles)
            card = GameCard(
                name,
                title,
                str(profile.get("backend") or ""),
                None,
                fallback_seed=title,
                parent=self._container,
            )
            card.clicked.connect(self.open_game.emit)
            self._flow.addWidget(card)
            self._cards[name] = card
            self._request_art(name, card, title, profile_art_candidates(profile))

        add = AddGameCard(self._container)
        add.clicked.connect(self.add_game.emit)
        self._flow.addWidget(add)
        count = len(self._cards)
        self.hint.setText(count_text(count, "game"))
        if count:
            self.subtitle.setText("Choose a game to launch it, or add a new one.")
        else:
            self.subtitle.setText(
                "No games yet. Click Add game to install your first one. You can add "
                "Chaos Zero Nightmare, Arknights or Stella Sora."
            )
        # Not adjustSize(): while the page is hidden (a removal runs from the game
        # page) that shrinks the container to one card wide, and nothing re-flows it.
        self._flow.invalidate()

    def _request_art(self, name: str, card: GameCard, title: str, candidates: list[str]) -> None:
        """Decode one cover in the background, then update its card if still shown."""
        width, height = CARD_WIDTH - 2, CARD_ART_H

        def apply(pixmap) -> None:
            if self._cards.get(name) is not card:
                return  # the grid was rebuilt while the decode ran
            try:
                card.art.setPixmap(cover_pixmap(pixmap, width, height, title, radius=13))
            except RuntimeError:
                pass  # the card was deleted before the callback arrived

        self._images.request(candidates, width, height, apply, cover=True)

    def wait_for_images(self, timeout_ms: int = 5000) -> None:
        """Wait for pending cover decodes (tests and shutdown helpers)."""
        self._images.wait(timeout_ms)

    def _clear(self) -> None:
        self._cards.clear()
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ---- state -------------------------------------------------------------
    def set_state(self, profile: str, state: str, running: bool = False) -> None:
        card = self._cards.get(profile)
        if card is not None:
            card.set_state(state, running)

    def clear_states(self) -> None:
        for card in self._cards.values():
            card.set_state("unknown", False)


__all__ = [
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
