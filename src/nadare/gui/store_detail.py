# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Detail view for a built-in store entry."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeyEvent, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import diskspace, steamstore
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


class _HeroLabel(QLabel):
    """A label that says when its size changed, so the art can be drawn at the new size."""

    resized = pyqtSignal()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.resized.emit()


class StoreDetail(QWidget):
    """Present catalog facts and emit navigation/install requests."""

    install_requested = pyqtSignal(str)
    play_requested = pyqtSignal(str)
    back_requested = pyqtSignal()
    #: The user picked another build of the same game (the kind of that build).
    variant_requested = pyqtSignal(str)

    def __init__(self, entry: StoreEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._entry = entry
        self._opened = False  # set_entry has not run yet
        self._hero: QPixmap | None = None
        self._hero_is_cover = False
        self._variants: tuple[StoreEntry, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        self.back_button = QPushButton("←  Back")
        self.back_button.setAccessibleName("Back to the store")
        self.back_button.setObjectName("Secondary")
        self.back_button.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.hero_label = _HeroLabel()
        self.hero_label.setObjectName("StoreHero")
        self.hero_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The art is re-rendered to the page width; it must not set that width itself.
        self.hero_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hero_label.setMinimumHeight(160)
        self.hero_label.setMaximumHeight(300)
        # The art is drawn at the label's own size (never stretched), so follow its resizes.
        self.hero_label.resized.connect(self._on_hero_resized)
        layout.addWidget(self.hero_label)

        self.title_label = QLabel()
        self.title_label.setObjectName("HeroTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.title_label)

        self.variant_box = QComboBox()
        self.variant_box.setAccessibleName("Version")
        self.variant_box.setToolTip("This game comes in more than one version; pick one.")
        self.variant_box.hide()
        self.variant_box.currentIndexChanged.connect(self._variant_picked)
        layout.addWidget(self.variant_box, 0, Qt.AlignmentFlag.AlignLeft)

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
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("Primary")
        self.play_button.setMinimumHeight(44)
        self.play_button.clicked.connect(lambda: self.play_requested.emit(self._entry.kind))
        self.install_button = QPushButton("Install")
        self.install_button.setObjectName("Primary")
        self.install_button.setMinimumHeight(44)
        self.install_button.clicked.connect(lambda: self.install_requested.emit(self._entry.kind))
        actions = QHBoxLayout()
        actions.addWidget(self.play_button)
        actions.addWidget(self.install_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        self.install_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.play_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.install_button.setFocus()
        self.set_entry(entry)

    @property
    def entry(self) -> StoreEntry:
        """The catalog entry currently shown."""
        return self._entry

    def set_entry(self, entry: StoreEntry, variants: tuple[StoreEntry, ...] | None = None) -> None:
        """Replace all entry content and restore its title placeholder art.

        ``variants`` are all the builds of this game (a version picker is shown for more than
        one). ``None`` keeps the builds already known, as long as ``entry`` is one of them.
        """
        # A refresh of the same build (it only became installed, say) keeps the art already
        # fetched; showing another build starts over, as its art is fetched afresh.
        kind_changed = self._entry.kind != entry.kind
        # Focus moves to Play only when the page opens, shows another build, or the build the
        # user just installed (focus on Install) becomes installed. A background refresh of the
        # same build must leave the user's place alone (#942).
        move_focus = (
            entry.installed
            and (
                not self._opened
                or kind_changed
                or (not self._entry.installed and self.focusWidget() is self.install_button)
            )
        )
        self._opened = True
        if kind_changed:
            self._hero = None
            self._hero_is_cover = False
        self._entry = entry
        if variants is None:
            variants = self._variants if any(v.kind == entry.kind for v in self._variants) else ()
        self._variants = tuple(variants)
        self._fill_variants(entry)
        self.title_label.setText(entry.group or entry.title)
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
        steam_tile = steamstore.app_id_of(entry.kind) is not None
        # A Steam tile reopens its one game; a catalog game can only be added once, so an
        # installed catalog game offers Play alone.
        self.install_button.setText("Open" if steam_tile and entry.installed else "Install")
        self.install_button.setVisible(not entry.installed or steam_tile)
        self._restyle(self.install_button, "GhostButton" if entry.installed else "Primary")
        self.play_button.setVisible(entry.installed)
        if move_focus:
            self.play_button.setFocus()
        self.installed_note.setVisible(entry.installed)
        self._render_hero()

    @staticmethod
    def _restyle(button: QPushButton, name: str) -> None:
        """Switch a button to another stylesheet id and repaint it."""
        if button.objectName() == name:
            return
        button.setObjectName(name)
        button.style().unpolish(button)
        button.style().polish(button)

    def _fill_variants(self, entry: StoreEntry) -> None:
        self.variant_box.blockSignals(True)
        self.variant_box.clear()
        if len(self._variants) > 1:
            for variant in self._variants:
                self.variant_box.addItem(variant.variant or variant.title)
            current = next((i for i, v in enumerate(self._variants) if v.kind == entry.kind), 0)
            self.variant_box.setCurrentIndex(current)
            self.variant_box.show()
        else:
            self.variant_box.hide()
        self.variant_box.blockSignals(False)

    def _variant_picked(self, index: int) -> None:
        if 0 <= index < len(self._variants) and self._variants[index].kind != self._entry.kind:
            self.variant_requested.emit(self._variants[index].kind)

    def set_hero(self, pixmap: QPixmap | None, *, is_cover: bool = False) -> None:
        """Set caller-provided artwork, or restore the catalog placeholder.

        ``is_cover`` says the image is the game's cover standing in for a hero, so it is
        fitted to the banner instead of simply cropped.
        """
        self._hero = pixmap
        self._hero_is_cover = is_cover and pixmap is not None
        self._render_hero()

    def _render_hero(self) -> None:
        label = self.hero_label
        height = max(label.minimumHeight(), min(label.maximumHeight(), label.height()))
        width = max(1, self.width() - 48)
        if self._hero is not None and not self._hero.isNull() and self._hero_is_cover:
            label.setPixmap(theme.banner_from_cover(self._hero, width, height))
            return
        label.setPixmap(theme.cover_pixmap(self._hero, width, height, self._entry.title))

    def _on_hero_resized(self) -> None:
        pixmap = self.hero_label.pixmap()
        if pixmap is None or pixmap.isNull() or pixmap.height() != self.hero_label.height():
            self._render_hero()

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
