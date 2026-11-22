# SPDX-License-Identifier: GPL-3.0-or-later
"""The one confirm dialog behind a Store card (#1536: the Store has no detail page)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import libraries
from .theme import apply_theme

#: Qt's QWIDGETSIZE_MAX: no maximum size.
_UNLIMITED = 16_777_215

#: ``exec()`` result when the user picked another version of the game: the caller opens the
#: dialog again for :attr:`QuickInstallDialog.variant_kind` (its size and folder differ).
VARIANT_CHANGED = 2

#: One line on what each way of delivering a game means (was on the Store detail page).
DELIVERY_FACTS = {
    "Direct download": "Installs straight from the publisher's servers, without their launcher.",
    "Publisher's launcher": (
        "Runs the publisher's own launcher under Wine/Proton. "
        "Anti-cheat or launcher updates can break it."
    ),
    # The backend's own notice carries the podman/binder requirements.
    "Android container": "Runs in an Android container (Lepton).",
}


@dataclass(frozen=True)
class StoreFacts:
    """What the Store knows about a game, shown before it is installed (#1536)."""

    tier: str = ""
    tier_help: str = ""
    delivery: str = ""
    #: ``(text, url)`` or ``(text, url, link label)``: the game's own notice.
    notice: tuple[str, ...] | None = None


def facts_for(kind: str) -> StoreFacts:
    """The Store catalog's facts for ``kind`` (empty facts for a kind the Store does not list)."""
    from ..core.storecatalog import entries

    entry = next((e for e in entries() if e.kind == kind), None)
    if entry is None:
        return StoreFacts()
    return StoreFacts(entry.tier, entry.tier_help, entry.delivery_label, entry.notice)


def _elide_middle(text: str, limit: int = 64) -> str:
    """``text`` cut in the middle with an ellipsis once it is longer than ``limit``."""
    if len(text) <= limit:
        return text
    head = limit // 2 - 3
    return f"{text[:head]}…{text[-(limit - head - 1):]}"



_LIST_ITEM = re.compile(r"\s*(?:[-*\u2022]|\d+[.)])\s")


def _reflow(text: str) -> str:
    """Join hard-wrapped lines into paragraphs; blank lines and list items stay."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines: list[str] = []
        for line in block.splitlines():
            if lines and not _LIST_ITEM.match(line):
                lines[-1] = f"{lines[-1].rstrip()} {line.strip()}"
            else:
                lines.append(line.rstrip())
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)

class QuickInstallDialog(QDialog):
    """Size, free space, the unofficial-launcher and account notices, the install folder, once.

    ``describe(chosen)`` returns ``(folder, free, summary)`` for the folder the user picked
    (``None`` = the default), so what is shown is where the game will really go.
    ``summary_text``, ``notice_text`` and ``account_notice_text`` are empty when there is
    nothing to say.
    """

    def __init__(
        self,
        title: str,
        *,
        notice: str,
        describe: Callable[[str | None], tuple[str, str, str]],
        parent: QWidget | None = None,
        choice: Any = None,
        choice_sizes: Mapping[str, int] | None = None,
        on_choice: Callable[[str], None] | None = None,
        libraries: list[tuple[str, str]] | None = None,
        facts: StoreFacts | None = None,
        variants: Sequence[tuple[str, str]] = (),
        current_kind: str = "",
        account_notice: str = "",
        change_location: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        apply_theme(self)
        self.setWindowTitle(f"Install {title}")
        self.setMinimumWidth(520)
        # A short question: maximised, it stretched into a strip of empty space. The flag is
        # not enough on Wayland; _fit_height also fixes the size, which KWin does honour.
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, False)
        self._describe = describe
        self.notice_text = notice
        self.account_notice_text = account_notice
        self.chosen_folder = ""
        # An unreadable default library must not escape the constructor (#1207 b212): say so
        # and keep Install off until a usable folder is picked.
        self._default_unreadable = ""
        try:
            self.folder_text, self._free, self.summary_text = describe(None)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            self._default_unreadable = reason
            self.folder_text, self._free = "", ""
            self.summary_text = (
                f"Nadare cannot use the default install folder ({reason}). Pick another folder."
            )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(f"Install {title}?")
        heading.setObjectName("Title")
        layout.addWidget(heading)

        # Account notices can be long. Keep the action buttons outside the scrolling
        # content so the combined sheet remains usable on a short display.
        outer_layout = layout
        self._content_scroll: QScrollArea | None = None
        if account_notice:
            self._content_scroll = QScrollArea()
            self._content_scroll.setWidgetResizable(True)
            self._content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)
            self._content_scroll.setWidget(content)
            outer_layout.addWidget(self._content_scroll)

        self._add_facts(layout, facts or StoreFacts())
        self.variant_kind = current_kind
        self.variant_combo: QComboBox | None = None
        if len(variants) > 1:
            self._add_variants(layout, variants, current_kind)

        self.summary_label = self._label("")
        layout.addWidget(self.summary_label)
        self.choice_combo: QComboBox | None = None
        self._on_choice = on_choice
        if choice is not None:
            self._add_choice(layout, choice, choice_sizes or {})
        if notice:
            head = QLabel("Nadare is an unofficial launcher")
            head.setStyleSheet("font-weight: 600;")
            layout.addWidget(head)
            layout.addWidget(self._label(_reflow(notice)))

        if account_notice:
            head = QLabel("Account notice")
            head.setStyleSheet("font-weight: 600;")
            layout.addWidget(head)
            layout.addWidget(self._label(_reflow(account_notice)))

        self.library_combo: QComboBox | None = None
        if libraries:
            # (label, folder) per library; the default library's folder is "".
            lib_row = QHBoxLayout()
            lib_row.addWidget(QLabel("Library"))
            combo = QComboBox()
            combo.setAccessibleName("Library")
            for label, folder in libraries:
                combo.addItem(label, folder)
                combo.setItemData(combo.count() - 1, folder or label, Qt.ItemDataRole.ToolTipRole)
            combo.currentIndexChanged.connect(self._library_changed)
            lib_row.addWidget(combo, 1)
            layout.addLayout(lib_row)
            self.library_combo = combo
        self.folder_label = self._label("")
        layout.addWidget(self.folder_label)
        row = QHBoxLayout()
        self.change_button = QPushButton("Change install location…")
        self.change_button.setAutoDefault(False)
        self.change_button.clicked.connect(self.change_location)
        self.change_button.setVisible(change_location)
        row.addWidget(self.change_button)
        row.addStretch(1)
        layout.addLayout(row)
        self._show_folder()

        buttons = QDialogButtonBox()
        self.install_button = buttons.addButton(
            "Install",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.install_button.setDefault(True)
        self.install_button.setEnabled(not self._default_unreadable)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer_layout.addWidget(buttons)

    def _add_facts(self, layout: QVBoxLayout, facts: StoreFacts) -> None:
        """Tier, delivery and the game's notice (with its link): what the Store detail page
        used to show, now before the one Install button (#1536)."""
        self.tier_label = QLabel(facts.tier)
        self.tier_label.setObjectName("Accent")
        self.tier_label.setVisible(bool(facts.tier))
        layout.addWidget(self.tier_label)
        self.tier_help_label = self._label(facts.tier_help)
        self.tier_help_label.setVisible(bool(facts.tier_help))
        layout.addWidget(self.tier_help_label)
        explanation = DELIVERY_FACTS.get(facts.delivery)
        delivery = f"{facts.delivery} — {explanation}" if explanation else facts.delivery
        self.delivery_label = self._label(delivery)
        self.delivery_label.setVisible(bool(delivery))
        layout.addWidget(self.delivery_label)
        notice = facts.notice or ()
        self._notice_url = str(notice[1]) if len(notice) > 1 else ""
        self.page_notice_label = self._label(str(notice[0]) if notice else "")
        self.page_notice_label.setVisible(bool(notice))
        layout.addWidget(self.page_notice_label)
        self.page_notice_link = QPushButton(
            (str(notice[2]) if len(notice) > 2 else "Learn more") if self._notice_url else ""
        )
        self.page_notice_link.setObjectName("Link")
        self.page_notice_link.setAutoDefault(False)
        self.page_notice_link.clicked.connect(self._open_notice)
        self.page_notice_link.setVisible(bool(self._notice_url))
        layout.addWidget(self.page_notice_link, 0, Qt.AlignmentFlag.AlignLeft)

    def _open_notice(self) -> None:
        if self._notice_url:
            QDesktopServices.openUrl(QUrl(self._notice_url))

    def _add_variants(
        self, layout: QVBoxLayout, variants: Sequence[tuple[str, str]], current: str
    ) -> None:
        """The versions of one game (GFL2: Darkwinter or Haoplay); another one reopens."""
        row = QHBoxLayout()
        row.addWidget(QLabel("Version"))
        combo = QComboBox()
        combo.setAccessibleName("Version")
        for label, kind in variants:
            combo.addItem(label, kind)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(self._variant_changed)
        row.addWidget(combo, 1)
        layout.addLayout(row)
        self.variant_combo = combo

    def _variant_changed(self, _index: int) -> None:
        if self.variant_combo is None:
            return
        picked = str(self.variant_combo.currentData() or "")
        if picked and picked != self.variant_kind:
            self.variant_kind = picked
            self.done(VARIANT_CHANGED)

    def _add_choice(self, layout: QVBoxLayout, option: Any, sizes: Mapping[str, int]) -> None:
        """A backend's install-time choice (WuWa's quality), each value with its size (#990)."""
        from ..core.diskspace import human_size

        row = QHBoxLayout()
        row.addWidget(QLabel(option.label))
        combo = QComboBox()
        for label, value in option.choices or ():
            size = sizes.get(value)
            combo.addItem(f"{label}, about {human_size(size)}" if size else label, value)
        combo.setAccessibleName(option.label)
        index = combo.findData(option.default)
        combo.setCurrentIndex(index if index >= 0 else 0)
        self._applied_choice = combo.currentData()
        combo.currentIndexChanged.connect(self._choice_changed)
        row.addWidget(combo)
        row.addStretch(1)
        layout.addLayout(row)
        if option.help:
            layout.addWidget(self._label(option.help))
        self.choice_combo = combo

    @property
    def choice_value(self) -> str | None:
        return None if self.choice_combo is None else self.choice_combo.currentData()

    def _choice_changed(self, _index: int) -> None:
        if self._on_choice is not None and self.choice_value is not None:
            self._on_choice(self.choice_value)
        try:
            self.folder_text, self._free, self.summary_text = self._describe(
                self.chosen_folder or None
            )
        except OSError as exc:
            # The shown numbers belong to the previous choice: put it back rather than show a
            # size that is not the one picked (#1207 b213).
            self._restore_choice(exc)
            return
        self._applied_choice = self.choice_value
        self._show_folder()

    def _restore_choice(self, exc: OSError) -> None:
        combo = self.choice_combo
        previous = getattr(self, "_applied_choice", None)
        if combo is not None:
            index = combo.findData(previous)
            if index >= 0 and index != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
                if self._on_choice is not None and previous is not None:
                    self._on_choice(previous)
        reason = exc.strerror or str(exc)
        self.summary_label.setText(f"Nadare cannot work out that choice's size ({reason}).")
        self.summary_label.setVisible(True)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height()
        if self.install_button.isEnabled():
            # Focus on Install, as Enter does: the notice's link came first in the tab order
            # and took the focus, so Space opened a web page (#1536).
            self.install_button.setFocus()

    def _fit_height(self) -> None:
        """Grow to the height the wrapped text needs at the width the dialog really has.

        The size hint is worked out at the layout's preferred width; shown narrower, the
        notice wraps to more lines and its first lines were cut off under the heading.
        """
        layout = self.layout()
        if layout is None:
            return
        # Free the size the last fit fixed, fit again, then fix it: a fixed size is what keeps
        # the dialog from being maximised on KDE Wayland (2026-11-20 user simulation).
        self.setMinimumSize(520, 0)
        self.setMaximumSize(_UNLIMITED, _UNLIMITED)
        try:
            layout.activate()
            if self._content_scroll is not None:
                scroll = self._content_scroll
                content = scroll.widget()
                need = content.layout().totalHeightForWidth(scroll.viewport().width())
                content.setMinimumHeight(need)
                screen = self.screen()
                available = screen.availableGeometry().height() - 60
                self.resize(self.width(), min(need + layout.minimumSize().height(), available))
                return
            need = layout.totalHeightForWidth(self.width())
            if need > self.height():
                self.resize(self.width(), need)
        finally:
            self.setFixedSize(self.size())

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("Hint")
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def _show_folder(self) -> None:
        extra = f" ({self._free})" if self._free else ""
        shown = _elide_middle(self.folder_text)
        self.folder_label.setText(f"Install folder: {shown}{extra}")
        # A path has no spaces to wrap at, so a long one would make the dialog very wide.
        self.folder_label.setToolTip(self.folder_text if shown != self.folder_text else "")
        self.summary_label.setText(self.summary_text)
        self.summary_label.setVisible(bool(self.summary_text))
        if self.isVisible():
            self._fit_height()

    def change_location(self) -> None:
        start = self.chosen_folder or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if chosen:
            # A library's own "Nadare" folder means that library: select it, not a new entry.
            chosen = str(libraries.library_of(chosen))
        if not chosen or not self._use_folder(chosen):
            return
        combo = self.library_combo
        if combo is not None:
            index = combo.findData(chosen)
            if index < 0:
                combo.addItem(chosen, chosen)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _library_changed(self, _index: int) -> None:
        combo = self.library_combo
        if combo is None:
            return
        folder = combo.currentData() or ""
        if not self._use_folder(folder or None):
            combo.blockSignals(True)
            back = combo.findData(self.chosen_folder)
            combo.setCurrentIndex(back if back >= 0 else 0)
            combo.blockSignals(False)

    def _use_folder(self, chosen: str | None) -> bool:
        """Show what installing into ``chosen`` (``None``: the default library) means."""
        try:
            folder_text, free, summary = self._describe(chosen)
        except OSError as exc:
            # A folder we may not look into: keep the one in use, and put the page's state back
            # (describe records the pick before it fails).
            try:
                self._describe(self.chosen_folder or None)
            except OSError:
                pass
            reason = exc.strerror or str(exc)
            self.summary_label.setText(
                f"Nadare cannot use {chosen} ({reason}). Pick another folder."
            )
            self.summary_label.setVisible(True)
            self._fit_height()
            return False
        self.chosen_folder = chosen or ""
        self.folder_text, self._free, self.summary_text = folder_text, free, summary
        self._default_unreadable = ""
        if hasattr(self, "install_button"):
            self.install_button.setEnabled(True)
        self._show_folder()
        return True
