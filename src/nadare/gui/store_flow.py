# SPDX-License-Identifier: GPL-3.0-or-later
"""Navigation between the store grid and a game's detail page."""

from __future__ import annotations

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QStackedWidget

from ..core import artwork
from ..core.storecatalog import entries, game_of
from .images import ImageLoader
from .store import StorePage
from .store_detail import StoreDetail


class _HeroSignals(QObject):
    finished = pyqtSignal(int, str, object)  # generation, kind, path


class _FetchHero(QRunnable):
    """Fetch one hero image off the GUI thread.

    ``signals`` belongs to the flow, never to the task: Qt destroys a finished runnable on
    a pool thread, and a QObject made on the GUI thread must not die there.
    """

    def __init__(self, generation: int, kind: str, title: str, signals: _HeroSignals) -> None:
        super().__init__()
        self.generation = generation
        self.kind = kind
        self.title = title
        self.signals = signals

    def run(self) -> None:
        try:
            path = artwork.fetch({"backend": self.kind}, self.title, "hero")
        except Exception:  # noqa: BLE001 - artwork is optional
            path = None
        try:
            self.signals.finished.emit(self.generation, self.kind, path)
        except RuntimeError:  # the flow was destroyed while the fetch ran
            pass


class StoreFlow(QStackedWidget):
    install_requested = pyqtSignal(str)
    own_game_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.grid = StorePage()
        self.addWidget(self.grid)
        self.detail: StoreDetail | None = None
        self._detail_kind = ""
        self._installed = frozenset()
        self._generation = 0
        self._images = ImageLoader(parent=self)
        self._hero_signals = _HeroSignals(self)
        self._hero_signals.finished.connect(self._hero_fetched)
        self.grid.entry_activated.connect(self.show_detail)
        self.grid.own_game_requested.connect(self.own_game_requested.emit)

    def set_installed_kinds(self, kinds: frozenset[str]) -> None:
        self._installed = frozenset(kinds)
        self.grid.set_installed_kinds(self._installed)
        if self.detail is None:
            return
        entry = self._entry_for(self._detail_kind)
        # Only rebuild the page when what it shows changed; set_entry resets the hero art.
        if entry is not None and entry != self.detail.entry:
            self.detail.set_entry(entry)

    def _entry_for(self, kind: str):
        return next((e for e in entries(installed_kinds=self._installed) if e.kind == kind), None)

    def show_detail(self, kind: str) -> None:
        game = game_of(kind, installed_kinds=self._installed)
        entry = next((e for e in game.entries if e.kind == kind), None) if game else None
        if entry is None:
            entry = self._entry_for(kind)
        if entry is None:
            return
        variants = game.entries if game is not None else ()
        self._generation += 1
        self._detail_kind = kind
        if self.detail is None:
            self.detail = StoreDetail(entry)
            self.addWidget(self.detail)
            self.detail.back_requested.connect(lambda: self.show_grid(focus_card=True))
            self.detail.install_requested.connect(self.install_requested.emit)
            self.detail.variant_requested.connect(self.show_detail)
            self.detail.set_entry(entry, variants)
        else:
            self.detail.set_entry(entry, variants)
        self.setCurrentWidget(self.detail)
        if entry.has_art:
            QThreadPool.globalInstance().start(
                _FetchHero(self._generation, kind, entry.title, self._hero_signals)
            )

    def _hero_fetched(self, generation: int, kind: str, path: object) -> None:
        if generation != self._generation or not path or self.detail is None:
            return
        self._images.request([str(path)], 1600, 400, lambda pixmap: self._set_hero(
            generation, kind, pixmap
        ))

    def _set_hero(self, generation: int, kind: str, pixmap: QPixmap | None) -> None:
        if (
            generation == self._generation
            and self.detail is not None
            and self._detail_kind == kind
        ):
            self.detail.set_hero(pixmap)

    def show_grid(self, *, focus_card: bool = False) -> None:
        """Return to the grid; ``focus_card`` puts focus back on the game that was open."""
        self._generation += 1
        self.setCurrentWidget(self.grid)
        if focus_card and self._detail_kind:
            self.grid.focus_card(self._detail_kind)
