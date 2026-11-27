# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The library: compact covers, recent games and favourites.

The library is presentation only. It renders whatever profiles the config holds
and shows a state pill that the main window feeds with ``Status`` objects.

The card widgets live in :mod:`nadare.gui.library_cards` (#1601) and are imported
back here, so callers keep using ``nadare.gui.library``.
"""

from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import gamefacts
from ..core.backends import Status
from ..core.profilenames import profile_display
from .game_order import game_sort_key, is_installed
from .images import ImageLoader
from .library_cards import (
    _BACKEND_LABELS,
    _FOCUS_RING,
    CARD_ART_H,
    CARD_HEIGHT,
    CARD_SCALE,
    CARD_WIDTH,
    NEW_DAYS,
    RECENT_MAX,
    TILE_ART_H,
    TILE_HEIGHT,
    TILE_WIDTH,
    AddGameCard,
    CoverLabel,
    FlowLayout,
    GameCard,
    RecentTile,
    TileLayout,
    _ago,
    _drop_shadow,
    _elide_name,
    _is_new,
    _name_clear_of_play,
    _paint_scrim,
    _parse_stamp,
    _place_badge,
    _tile_text,
    backend_label,
    card_width_for,
    tile_width_for,
)
from .theme import scaled


def profile_title(name: str, profile: dict[str, Any]) -> str:
    """The display title for a profile (falls back to the profile name)."""
    return str(profile.get("title") or name)


def display_title(name: str, profiles: dict[str, dict[str, Any]]) -> str:
    """Like :func:`profile_title`, plus the profile id when another profile shares it."""
    return profile_display(name, profiles.get(name) or {}, profiles)


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


def recent_art_candidates(profile: dict[str, Any]) -> list[str]:
    """Use recorded wide art and the store's cached cover-wide slot, without fetching."""
    from ..core import artwork

    paths = artwork.ordered_paths(profile, ("custom_background", "wide", "hero"))
    cached = artwork.cached(artwork.slug(profile, profile.get("title")), "cover-wide")
    if cached is not None:
        paths.append(str(cached))
    return list(dict.fromkeys(paths + profile_art_candidates(profile)))


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
    "_BACKEND_LABELS",
    "_FOCUS_RING",
    "_ago",
    "_drop_shadow",
    "_elide_name",
    "_is_new",
    "_name_clear_of_play",
    "_paint_scrim",
    "_parse_stamp",
    "_place_badge",
    "_tile_text",
    "AddGameCard",
    "CARD_ART_H",
    "CARD_HEIGHT",
    "CARD_SCALE",
    "CARD_WIDTH",
    "CoverLabel",
    "FlowLayout",
    "GameCard",
    "LibraryPage",
    "NEW_DAYS",
    "RECENT_MAX",
    "RecentTile",
    "TILE_ART_H",
    "TILE_HEIGHT",
    "TILE_WIDTH",
    "TileLayout",
    "backend_label",
    "card_width_for",
    "count_text",
    "display_title",
    "profile_art_candidates",
    "profile_title",
    "recent_art_candidates",
    "tile_width_for",
]
