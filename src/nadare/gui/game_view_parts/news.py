# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The news cards under the game.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt

from ...core.backends import (
    supports_news,
)
from .shared import (
    NewsCard,
)


class NewsMixin:
    """Mixin for :class:`GameView`; see the module text."""

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
            # Every field a card shows, or a re-dated or re-categorised article keeps its old
            # values after a silent refresh (#975).
            tuple(
                (
                    item.get("id"),
                    item.get("title"),
                    item.get("image_path"),
                    item.get("url"),
                    item.get("category"),
                    item.get("published_at"),
                )
                for item in items
            ),
        )
        if items and self.news_rows and key == self._news_key:
            return  # a silent refresh with the same items: keep the cards, no flash
        self._clear_news()
        self._news_key = key
        for item in items:
            card = NewsCard(item, self.news_frame, loader=self.images, fallback=self._news_fallback)
            card.clicked.connect(self._open_external)
            self.news_rows.append(card)
        if self.news_rows:
            self.news_hint.setVisible(False)
            self.news_reason.setVisible(False)
            self.news_retry.setVisible(False)
            self._set_news_visible(True)
            self._relayout_news()
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
