# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The sidebar's installed-games list.

Only profiles the backend last reported as installed (or running, or with an
update available) get a row here; everything else lives on the Library page.
The widget is presentation only — it renders the ``Status`` objects the main
window forwards and emits :attr:`SidebarGames.open_game` when a row is clicked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.backends import Status
from .library import display_title, profile_art_candidates
from .theme import cover_pixmap, load_card_pixmap, repolish

ICON_SIZE = 28
ROW_HEIGHT = 38


def is_installed(status: Status | None) -> bool:
    """True when ``status`` means the game is on disk / running / updatable."""
    if status is None:
        return False
    state = (status.state or "").strip().lower()
    if state in ("installed", "running"):
        return True
    return bool((status.data or {}).get("update_available"))


def _running(status: Status | None) -> bool:
    if status is None:
        return False
    return bool(status.running or (status.state or "").strip().lower() == "running")


def _updatable(status: Status | None) -> bool:
    if status is None:
        return False
    return bool((status.data or {}).get("update_available"))


def icon_pixmap(profile: dict[str, Any], title: str):
    """A small square icon: the logo, then cover/hero, then a placeholder."""
    candidates: list[str] = []
    logo = profile.get("logo")
    if logo:
        # ``logo`` is not is_file()-checked like profile_art_candidates: a FIFO
        # or other non-regular file would make QPixmap block while it reads.
        try:
            if Path(str(logo)).expanduser().is_file():
                candidates.append(str(logo))
        except (TypeError, ValueError, RuntimeError, OSError):
            pass  # includes RuntimeError from an unresolvable ``~user``
    candidates.extend(profile_art_candidates(profile))
    art = load_card_pixmap(candidates)
    return cover_pixmap(art, ICON_SIZE, ICON_SIZE, title, radius=8)


class GameRow(QFrame):
    """One installed game: icon, elided title and a running/update dot."""

    clicked = pyqtSignal(str)

    def __init__(
        self,
        profile: str,
        title: str,
        art: Any = None,
        *,
        active: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self._content_key: tuple[Any, ...] | None = None
        self.setObjectName("GameRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ROW_HEIGHT)
        self.setToolTip(title)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 8, 4)
        layout.setSpacing(9)

        self.icon = QLabel()
        self.icon.setObjectName("GameRowIcon")
        self.icon.setFixedSize(ICON_SIZE, ICON_SIZE)
        self.icon.setPixmap(
            art if art is not None else cover_pixmap(None, ICON_SIZE, ICON_SIZE, title, 8)
        )
        layout.addWidget(self.icon)

        metrics = QFontMetrics(self.font())
        self.title_label = QLabel(
            metrics.elidedText(str(title), Qt.TextElideMode.ElideRight, 150)
        )
        self.title_label.setObjectName("GameRowTitle")
        # Profile titles are user-controlled: never render them as rich text.
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setToolTip(Qt.convertFromPlainText(title))
        layout.addWidget(self.title_label, 1)

        self.dot = QLabel("")
        self.dot.setObjectName("GameRowDot")
        self.dot.setFixedSize(8, 8)
        self.dot.hide()
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.set_active(active)
        self.set_state(None)

    def update_content(self, profile: dict[str, Any], title: str) -> None:
        """Refresh title/icon in place when the profile changed; cheap otherwise."""
        key = (title, profile.get("logo"), tuple(profile_art_candidates(profile)))
        if key == self._content_key:
            return
        self._content_key = key
        metrics = QFontMetrics(self.font())
        self.title_label.setText(
            metrics.elidedText(str(title), Qt.TextElideMode.ElideRight, 150)
        )
        self.title_label.setToolTip(title)
        self.setToolTip(title)
        self.icon.setPixmap(icon_pixmap(profile, title))

    def set_state(self, status: Status | None) -> None:
        """Show the running dot (blue) or the update dot (amber)."""
        if _running(status):
            kind = "running"
        elif _updatable(status):
            kind = "update"
        else:
            kind = ""
        self.dot.setProperty("kind", kind)
        repolish(self.dot)
        self.dot.setVisible(bool(kind))
        label = {"running": "Running", "update": "Update available"}.get(kind, "")
        self.dot.setAccessibleName(label)
        self.dot.setToolTip(label)

    def set_active(self, active: bool) -> None:
        value = "true" if active else "false"
        self.setProperty("active", value)
        self.title_label.setProperty("active", value)
        repolish(self)
        repolish(self.title_label)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self.profile)
        super().mouseReleaseEvent(event)


class SidebarGames(QWidget):
    """Scrollable list of installed games, ordered running-first then A-Z."""

    open_game = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarGames")
        self._profiles: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, Status] = {}
        self._active = ""
        self._rows: dict[str, GameRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self.header = QLabel("GAMES")
        self.header.setObjectName("Section")
        root.addWidget(self.header)

        self._container = QWidget()
        self._container.setObjectName("SidebarGamesContainer")
        self._rows_layout = QVBoxLayout(self._container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("SidebarScroll")
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Tab goes to the items, not the frame
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self._container)
        root.addWidget(self.scroll, 1)

        self._rebuild()

    # ---- data --------------------------------------------------------------
    def set_profiles(
        self, profiles: dict[str, dict[str, Any]], statuses: dict[str, Status] | None = None
    ) -> None:
        """Replace the profile set, keeping (or replacing) the known statuses."""
        self._profiles = profiles
        if statuses is not None:
            self._statuses = dict(statuses)
        # A removed profile's status must not survive into a later profile that
        # happens to reuse the same name.
        known = set(profiles)
        self._statuses = {
            name: status for name, status in self._statuses.items() if name in known
        }
        self._rebuild()

    def set_status(self, profile: str, status: Status) -> None:
        """Record one backend status result and refresh the list live."""
        self._statuses[profile] = status
        self._rebuild()

    def set_active(self, profile: str) -> None:
        self._active = profile or ""
        for name, row in self._rows.items():
            row.set_active(name == self._active)

    # ---- inspection --------------------------------------------------------
    def row(self, profile: str) -> GameRow | None:
        return self._rows.get(profile)

    def profiles(self) -> list[str]:
        """The profiles currently shown, in display order."""
        return list(self._rows)

    # ---- rendering ---------------------------------------------------------
    def _ordered(self) -> list[str]:
        entries: list[tuple[int, str, str]] = []
        for name in self._profiles:
            status = self._statuses.get(name)
            if not is_installed(status):
                continue
            title = display_title(name, self._profiles)
            entries.append((0 if _running(status) else 1, title.lower(), name))
        entries.sort()
        return [name for _rank, _title, name in entries]

    def _rebuild(self) -> None:
        # Reuse rows keyed by profile: a status race must never delete the row
        # whose mouse handler is still on the stack (that raised RuntimeError).
        order = self._ordered()
        wanted = set(order)
        for name in list(self._rows):
            if name not in wanted:
                row = self._rows.pop(name)
                row.setParent(None)
                row.deleteLater()

        for index, name in enumerate(order):
            profile = self._profiles[name]
            title = display_title(name, self._profiles)
            row = self._rows.get(name)
            if row is None:
                row = GameRow(
                    name,
                    title,
                    icon_pixmap(profile, title),
                    active=(name == self._active),
                    parent=self._container,
                )
                row.clicked.connect(self.open_game.emit)
                self._rows[name] = row
            else:
                row.update_content(profile, title)
                row.set_active(name == self._active)
            row.set_state(self._statuses.get(name))
            self._rows_layout.insertWidget(index, row)

        self.header.setVisible(bool(self._rows))
        self._container.adjustSize()
        # Keep the mapping's order in sync with the visual order (profiles()).
        self._rows = {name: self._rows[name] for name in order}


__all__ = ["GameRow", "SidebarGames", "icon_pixmap", "is_installed"]
