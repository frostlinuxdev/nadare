# SPDX-License-Identifier: GPL-3.0-or-later
"""What a Store card does (#1536: there is no detail page between the grid and the install).

A game not installed asks for the Install dialog (which carries the tier, delivery, the game's
notice and the version picker); an installed game opens its page; a Steam tile goes to its own
add-or-open flow.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QStackedWidget

from ..core import steamstore
from ..core.storecatalog import game_of
from .store import StorePage


class StoreFlow(QStackedWidget):
    install_requested = pyqtSignal(str)
    play_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.grid = StorePage()
        self.addWidget(self.grid)
        self._installed: frozenset[str] = frozenset()
        self.grid.entry_activated.connect(self.activate)

    def set_installed_kinds(self, kinds: frozenset[str]) -> None:
        self._installed = frozenset(kinds)
        self.grid.set_installed_kinds(self._installed)

    def activate(self, kind: str) -> None:
        """A card was opened: play what is installed, otherwise ask to install ``kind``."""
        if steamstore.app_id_of(kind) is not None:
            self.install_requested.emit(kind)  # the window adds the tile or reopens it
            return
        game = game_of(kind, installed_kinds=self._installed)
        builds = game.entries if game is not None else ()
        installed = next((build.kind for build in builds if build.kind in self._installed), "")
        if not installed and kind in self._installed:
            installed = kind
        if installed:
            self.play_requested.emit(installed)
        else:
            self.install_requested.emit(kind)

    def show_grid(self, *, focus_card: bool = False) -> None:
        """Show the grid (it is the only page); kept for the window's Store button."""
        self.setCurrentWidget(self.grid)
