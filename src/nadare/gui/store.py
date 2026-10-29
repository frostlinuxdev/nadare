# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The built-in game store grid."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import artwork
from ..core.diskspace import human_size
from ..core.storecatalog import StoreEntry, store_games, unavailable_games
from .banner import InfoBanner
from .images import ImageLoader
from .library import FlowLayout, _drop_shadow
from .logs_dialog import LogsDialog
from .theme import ACCENT, cover_pixmap

#: Store tiles are landscape: most covers are wide key art, which then fills the tile whole.
CARD_WIDTH = 330
CARD_ART_H = 186
CARD_HEIGHT = CARD_ART_H + 148

_FOCUS_RING = f"QFrame#StoreCard:focus {{ border: 2px solid {ACCENT}; }}"

log = logging.getLogger(__name__)


class _FetchSignals(QObject):
    finished = pyqtSignal(str, object)


class _FetchArt(QRunnable):
    """Fetch one cover away from the GUI thread.

    The signals object belongs to the page, not to the runnable: Qt deletes a finished
    runnable on a pool thread, and a QObject made on the GUI thread must not die there.
    """

    def __init__(self, kind: str, title: str, signals: _FetchSignals) -> None:
        super().__init__()
        self.kind = kind
        self.title = title
        self.signals = signals

    def run(self) -> None:
        try:
            path = artwork.fetch({"backend": self.kind}, self.title, "cover")
        except Exception:  # noqa: BLE001 - missing art degrades to the placeholder
            path = None
        try:
            self.signals.finished.emit(self.kind, path)
        except RuntimeError:  # the page was destroyed while the fetch ran
            pass


class StoreCard(QFrame):
    """Focusable, clickable card for one built-in catalog entry."""

    activated = pyqtSignal(str)

    def __init__(
        self,
        entry: StoreEntry,
        parent: QWidget | None = None,
        *,
        art_title: str | None = None,
        art_kind: str | None = None,
        has_art: bool | None = None,
        kinds: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        #: The title the cover is looked up by (a game with several builds shows the game's name).
        self.art_title = art_title or entry.title
        #: Which build's cover this card shows (the game's lead build, whatever filter is on).
        self.art_kind = art_kind or entry.kind
        #: Whether that build has a cover to fetch (the lead build's, not a filtered one's).
        self.has_art = entry.has_art if has_art is None else has_art
        #: Every build this card stands for.
        self.kinds = kinds or (entry.kind,)
        self.setObjectName("StoreCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(entry.title)
        self.setToolTip(entry.title)
        self.setStyleSheet(_FOCUS_RING)
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self._shadow = _drop_shadow(self, 18, 70)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 0)
        root.setSpacing(0)

        self.art = QLabel()
        self.art.setFixedSize(CARD_WIDTH - 2, CARD_ART_H)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.art.setPixmap(cover_pixmap(None, CARD_WIDTH - 2, CARD_ART_H, entry.title, radius=13))
        root.addWidget(self.art)

        footer = QWidget()
        footer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content = QVBoxLayout(footer)
        content.setContentsMargins(14, 10, 14, 12)
        content.setSpacing(6)
        self.title_label = QLabel(entry.title)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.title_label.setToolTip(entry.title)
        content.addWidget(self.title_label)

        tags = QHBoxLayout()
        tags.setContentsMargins(0, 0, 0, 0)
        tags.setSpacing(6)
        self.tier_badge = QLabel(entry.tier)
        self.tier_badge.setObjectName("Pill")
        self.tier_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tier_badge.setToolTip(entry.tier_help)
        tags.addWidget(self.tier_badge, 0, Qt.AlignmentFlag.AlignLeft)
        tags.addStretch(1)
        self.added_marker = QLabel("Added")
        self.added_marker.setObjectName("Pill")
        self.added_marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.added_marker.setVisible(entry.installed)
        tags.addWidget(self.added_marker, 0, Qt.AlignmentFlag.AlignRight)
        content.addLayout(tags)

        self.delivery_label = QLabel(entry.delivery_label)
        self.delivery_label.setObjectName("CardMeta")
        self.delivery_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.delivery_label.setWordWrap(True)
        content.addWidget(self.delivery_label)

        self.size_label = QLabel(f"~{human_size(entry.size_bytes)}" if entry.size_bytes else "")
        self.size_label.setObjectName("CardMeta")
        self.size_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.size_label.setVisible(bool(entry.size_bytes))
        content.addWidget(self.size_label)
        content.addStretch(1)  # top-align, so a card without a size line matches its neighbours
        root.addWidget(footer)
        self._describe()

    def _describe(self) -> None:
        """What a screen reader says after the title: tier, delivery, size, added."""
        parts = [self.entry.tier, self.entry.delivery_label]
        if self.entry.size_bytes:
            parts.append(f"about {human_size(self.entry.size_bytes)}")
        if not self.added_marker.isHidden():
            parts.append("already in your library")
        self.setAccessibleDescription(". ".join(parts))

    def set_added(self, added: bool) -> None:
        self.added_marker.setVisible(added)
        self._describe()

    def set_art(self, pixmap: Any) -> None:
        self.art.setPixmap(
            cover_pixmap(pixmap, CARD_WIDTH - 2, CARD_ART_H, self.entry.title, radius=13)
        )

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._shadow.setBlurRadius(34)
        self._shadow.setColor(QColor(0, 0, 0, 120))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._shadow.setBlurRadius(18)
        self._shadow.setColor(QColor(0, 0, 0, 70))
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.entry.kind)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.activated.emit(self.entry.kind)
        super().mouseReleaseEvent(event)


class StorePage(QWidget):
    """Searchable and filterable grid of available built-in catalog games."""

    entry_activated = pyqtSignal(str)
    own_game_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._installed_kinds: frozenset[str] = frozenset()
        self._cards: dict[str, StoreCard] = {}
        #: Every build's kind -> the card of its game (a game with several builds has one card).
        self._card_for_kind: dict[str, StoreCard] = {}
        self._art_started = False
        self._built = False
        self._art_requested: set[str] = set()
        #: Fetched cover files by kind, so cards rebuilt by a search keep their art.
        self._art_paths: dict[str, str] = {}
        self._images = ImageLoader(parent=self)
        self._art_signals = _FetchSignals(self)
        self._art_signals.finished.connect(self._on_art_fetched)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(18)

        heading = QLabel("Store")
        heading.setObjectName("Title")
        root.addWidget(heading)

        self.unavailable_banner = InfoBanner()
        self.unavailable_banner.action_clicked.connect(self._open_logs)
        root.addWidget(self.unavailable_banner)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games")
        self.search.setAccessibleName("Search games")
        self.search.setClearButtonEnabled(True)
        controls.addWidget(self.search, 1)

        self.tier_filter = QComboBox()
        self.tier_filter.setAccessibleName("Filter by support tier")
        self.tier_filter.addItems(("All tiers", "Verified", "Experimental"))
        controls.addWidget(self.tier_filter)
        root.addLayout(controls)
        QWidget.setTabOrder(self.search, self.tier_filter)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("StoreScroll")
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._container.setObjectName("StoreContainer")
        self._flow = FlowLayout(self._container, margin=2, spacing=18)
        self._container.setLayout(self._flow)
        self.scroll.setWidget(self._container)

        self.empty_label = QLabel("No games match your search.")
        self.empty_label.setObjectName("Muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results = QStackedWidget()
        self.results.addWidget(self.scroll)
        self.results.addWidget(self.empty_label)
        root.addWidget(self.results, 1)

        self.own_game_button = QPushButton("Add your own game...")
        self.own_game_button.setObjectName("Link")
        self.own_game_button.setAccessibleName("Add your own game")
        self.own_game_button.clicked.connect(self.own_game_requested.emit)
        root.addWidget(self.own_game_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.search.textChanged.connect(self.refresh)
        self.tier_filter.currentTextChanged.connect(self.refresh)
        self._report_unavailable_games()

    def _open_logs(self) -> None:
        """Open the app log, where each unavailable backend was just named."""
        self._logs_dialog = LogsDialog([], parent=self)
        self._logs_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._logs_dialog.show()

    def _report_unavailable_games(self) -> None:
        """Name the games this build lost in the Store, and in the app log."""
        missing = unavailable_games()
        if not missing:
            self.unavailable_banner.hide_banner()
            return
        for kind, title in missing:
            log.warning(
                "backend %s (%s) is not available in this build; the Store cannot list it",
                kind,
                title,
            )
        count = len(missing)
        noun = "game" if count == 1 else "games"
        titles = ", ".join(title for _kind, title in missing)
        self.unavailable_banner.show_message(
            f"{count} {noun} can't load in this build: {titles}.",
            "Open logs",
            dismissible=False,
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._built:
            self._built = True
            self.refresh()
        self._art_started = True
        self._request_visible_art()

    def set_installed_kinds(self, kinds: frozenset[str]) -> None:
        self._installed_kinds = frozenset(kinds)
        for card in self._cards.values():
            card.set_added(any(kind in self._installed_kinds for kind in card.kinds))

    def refresh(self) -> None:
        """Rebuild cards using the headless model's filtering and ordering."""
        if not self._built:
            return
        selected_tier = (
            self.tier_filter.currentText() if hasattr(self, "tier_filter") else "All tiers"
        )
        tier = selected_tier if selected_tier in ("Verified", "Experimental") else None
        query = self.search.text() if hasattr(self, "search") else ""
        games = store_games(installed_kinds=self._installed_kinds, query=query, tier=tier)
        whole = {}
        for full in store_games(installed_kinds=self._installed_kinds):
            for build in full.entries:
                whole[build.kind] = full
        self._clear_cards()
        for game in games:
            primary = game.primary
            shown = primary
            if len(game.entries) > 1:
                shown = replace(
                    primary,
                    title=game.title,
                    delivery_label=f"{len(whole[primary.kind].entries)} versions",
                    size_bytes=None,
                    installed=whole[primary.kind].installed,
                )
            # The card stands for the whole game, whatever the search or filter hides of it.
            kinds = tuple(entry.kind for entry in whole[primary.kind].entries)
            art = whole[primary.kind].primary
            card = StoreCard(
                shown,
                self._container,
                art_title=art.title,
                art_kind=art.kind,
                has_art=art.has_art,
                kinds=kinds,
            )
            card.activated.connect(self.entry_activated.emit)
            self._flow.addWidget(card)
            self._cards[primary.kind] = card
            for kind in kinds:
                self._card_for_kind[kind] = card
            self._apply_art(card)
        results = games
        self._flow.invalidate()
        self.results.setCurrentWidget(self.scroll if results else self.empty_label)
        if results:
            QWidget.setTabOrder(self.tier_filter, self._cards[results[0].primary.kind])
            QWidget.setTabOrder(self._cards[results[-1].primary.kind], self.own_game_button)
        if self._art_started:
            self._request_visible_art()

    def focus_search(self) -> None:
        self.search.setFocus()

    def focus_card(self, kind: str) -> None:
        """Focus the card for ``kind``; fall back to the search box when it is not shown."""
        card = self._card_for_kind.get(kind) or self._cards.get(kind)
        (card or self.search).setFocus()

    def visible_kinds(self) -> list[str]:
        return list(self._cards)

    def wait_for_images(self, timeout_ms: int = 5000) -> None:
        """Wait for queued art fetches and image decodes (tests and shutdown helpers)."""
        from PyQt6.QtCore import QCoreApplication, QElapsedTimer, QThreadPool

        timer = QElapsedTimer()
        timer.start()
        QThreadPool.globalInstance().waitForDone(timeout_ms)
        while timer.elapsed() < timeout_ms:
            QCoreApplication.processEvents()
            if not self._images.pending():
                break

    def _request_visible_art(self) -> None:
        from PyQt6.QtCore import QThreadPool

        pool = QThreadPool.globalInstance()
        for card in tuple(self._cards.values()):
            if not card.has_art or card.art_kind in self._art_requested:
                continue
            self._art_requested.add(card.art_kind)
            pool.start(_FetchArt(card.art_kind, card.art_title, self._art_signals))

    def _on_art_fetched(self, kind: str, path: object) -> None:
        if not path:
            return
        self._art_paths[kind] = str(path)
        for card in tuple(self._cards.values()):
            if card.art_kind == kind:
                self._apply_art(card)

    def _apply_art(self, card: StoreCard) -> None:
        """Decode the fetched cover (if any) for ``card``."""
        path = self._art_paths.get(card.art_kind)
        if path is None:
            return

        def apply(pixmap) -> None:
            if card in self._cards.values():
                card.set_art(pixmap)

        self._images.request([path], CARD_WIDTH - 2, CARD_ART_H, apply, cover=True, fit=True)

    def _clear_cards(self) -> None:
        self._cards.clear()
        self._card_for_kind.clear()
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


__all__ = ["StoreCard", "StorePage"]
