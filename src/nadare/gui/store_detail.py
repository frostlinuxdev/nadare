# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Detail view for a built-in store entry."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeyEvent, QPixmap
from PyQt6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..core import diskspace
from ..core.storecatalog import StoreEntry
from . import theme

_DELIVERY_FACTS = {
    "Direct download": "Installs straight from the publisher's servers, without their launcher.",
    "Publisher's launcher": (
        "Runs the publisher's own launcher under Wine/Proton. "
        "Anti-cheat or launcher updates can break it."
    ),
    "Publisher's installer": "Runs the publisher's installer under Wine/Proton.",
    # The backend's own notice below carries the podman/binder requirements.
    "Android container": "Runs in an Android container (Lepton).",
}


class StoreDetail(QWidget):
    """Present catalog facts and emit navigation/install requests."""

    install_requested = pyqtSignal(str)
    back_requested = pyqtSignal()

    def __init__(self, entry: StoreEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._entry = entry
        self._hero: QPixmap | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        self.back_button = QPushButton("←  Back")
        self.back_button.setAccessibleName("Back to the store")
        self.back_button.setObjectName("Secondary")
        self.back_button.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.hero_label = QLabel()
        self.hero_label.setObjectName("StoreHero")
        self.hero_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The art is re-rendered to the page width; it must not set that width itself.
        self.hero_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hero_label.setScaledContents(True)
        self.hero_label.setMinimumHeight(160)
        self.hero_label.setMaximumHeight(300)
        layout.addWidget(self.hero_label)

        self.title_label = QLabel()
        self.title_label.setObjectName("HeroTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.title_label)

        self.tier_label = QLabel()
        self.tier_label.setObjectName("Accent")
        layout.addWidget(self.tier_label)
        self.tier_help_label = QLabel()
        self.tier_help_label.setObjectName("Hint")
        self.tier_help_label.setWordWrap(True)
        self.tier_help_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.tier_help_label)

        self.delivery_label = QLabel()
        self.delivery_label.setObjectName("Hint")
        self.delivery_label.setWordWrap(True)
        self.delivery_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.delivery_label)
        self.size_label = QLabel()
        self.size_label.setObjectName("Muted")
        self.size_label.setWordWrap(True)
        layout.addWidget(self.size_label)

        self.notice_label = QLabel()
        self.notice_label.setObjectName("Hint")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice_label)
        self.notice_link = QPushButton()
        self.notice_link.setObjectName("Link")
        self.notice_link.clicked.connect(self._open_notice)
        layout.addWidget(self.notice_link, 0, Qt.AlignmentFlag.AlignLeft)

        self.installed_note = QLabel("Already in your library.")
        self.installed_note.setObjectName("Muted")
        layout.addWidget(self.installed_note)
        self.install_button = QPushButton("Install")
        self.install_button.setObjectName("Primary")
        self.install_button.setMinimumHeight(44)
        self.install_button.clicked.connect(lambda: self.install_requested.emit(self._entry.kind))
        layout.addWidget(self.install_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.install_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.install_button.setFocus()
        self.set_entry(entry)

    @property
    def entry(self) -> StoreEntry:
        """The catalog entry currently shown."""
        return self._entry

    def set_entry(self, entry: StoreEntry) -> None:
        """Replace all entry content and restore its title placeholder art."""
        self._entry = entry
        self._hero = None
        self.title_label.setText(entry.title)
        self.tier_label.setText(entry.tier)
        self.tier_help_label.setText(entry.tier_help)
        explanation = _DELIVERY_FACTS.get(entry.delivery_label)
        delivery = entry.delivery_label
        if explanation:
            delivery = f"{delivery} — {explanation}"
        self.delivery_label.setText(delivery)
        if entry.size_bytes is None:
            self.size_label.clear()
            self.size_label.hide()
        else:
            self.size_label.setText(f"Install size: about {diskspace.human_size(entry.size_bytes)}")
            self.size_label.show()
        if entry.notice:
            text, url = entry.notice[0], entry.notice[1]
            label = entry.notice[2] if len(entry.notice) > 2 else "Learn more"
            self.notice_label.setText(text)
            self.notice_link.setText(label)
            self._notice_url = url
            self.notice_label.show()
            self.notice_link.show()
        else:
            self.notice_label.clear()
            self.notice_link.setText("")
            self._notice_url = ""
            self.notice_label.hide()
            self.notice_link.hide()
        self.install_button.setText("Add another" if entry.installed else "Install")
        self.installed_note.setVisible(entry.installed)
        self._render_hero()

    def set_hero(self, pixmap: QPixmap | None) -> None:
        """Set caller-provided artwork, or restore the catalog placeholder."""
        self._hero = pixmap
        self._render_hero()

    def _render_hero(self) -> None:
        self.hero_label.setPixmap(
            theme.cover_pixmap(self._hero, max(1, self.width() - 48), 240, self._entry.title)
        )

    def _open_notice(self) -> None:
        if self._notice_url:
            QDesktopServices.openUrl(QUrl(self._notice_url))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.back_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render_hero()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 420)

    def sizeHint(self) -> QSize:
        return QSize(520, 520)


__all__ = ["StoreDetail"]
