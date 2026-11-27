# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Settings search field: filter every tab by the text that is shown (#1810).

The page hides rows for its own reasons too (a Steam game has no Game folder row, advanced
options start hidden). The search therefore keeps its own set of rows and cards it hid, and
restores only those, to what the page last asked for. Clearing the field then leaves every
page-hidden row hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6 import sip
from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QWidget,
)

from .. import i18n

#: Hint rows: a muted label under the setting it explains.
_HINT_STYLES = frozenset({"Muted", "Subtitle"})

#: The no-hits line echoes only this much of the query: a long paste would otherwise
#: stretch the page and make every keystroke re-shape the whole 32 k string.
_QUERY_ECHO_CHARS = 200
#: Pixel width the echoed query is elided to (the page is never wider than the window).
_QUERY_ECHO_WIDTH = 480
#: Longest query the box takes: every word is matched against every row on each
#: keystroke, so a 32 k paste of short words stalled the page (Neon r1, 2026-11-26).
_QUERY_MAX_CHARS = 200


class SettingsSearchEdit(QLineEdit):
    """The search box; Escape clears it without leaving the field."""

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


class SettingsSearchForm(QFormLayout):
    """A form that remembers whether the page wanted each row shown.

    ``setRowVisible`` from the page is recorded here; the search hides rows with
    :meth:`hide_for_search` instead, so restoring never shows a row the page hid.
    The page also toggles single widgets directly (``runner_behind``, ``language``,
    the prefix buttons…). Its calls go through :meth:`SearchMixin._set_widget_visible`,
    which records the page's wish here and re-applies an active search afterwards; an
    event filter on every row widget catches any direct call that bypasses it.
    """

    def __init__(self, page: SearchMixin) -> None:
        super().__init__()
        self._search_page = page
        self._page_visible: dict[int, bool] = {}
        self._search_rows: set[int] = set()
        self._watchers: dict[QWidget, int] = {}
        self._owners: dict[QWidget, int] = {}

    def owner_row(self, widget: QWidget) -> int | None:
        """The row whose own label or field is ``widget`` (not a child of one)."""
        return self._owners.get(widget)

    def set_page_visible(self, row: int, visible: bool) -> None:
        """Record what the page wants for ``row`` (ignored while the search works)."""
        if self._search_page._search_applying:
            return
        self._page_visible[row] = bool(visible)
        if visible and not self.isRowVisible(row):
            # The search may have set Qt's own row-visible flag False. Showing the
            # page's widget does not clear it, so do it here and stop owning the row.
            super().setRowVisible(row, True)
            self._search_rows.discard(row)

    def page_visible(self, row: int) -> bool:
        """Whether the page itself wants ``row`` shown (before any search)."""
        if row in self._page_visible:
            return self._page_visible[row]
        label, field = _row_widgets(self, row)
        return not (
            (label is not None and label.isHidden()) or (field is not None and field.isHidden())
        )

    def hide_for_search(self, row: int) -> None:
        if not self.isRowVisible(row):
            return
        self._page_visible.setdefault(row, True)
        super().setRowVisible(row, False)
        self._search_rows.add(row)

    def restore_search_rows(self) -> None:
        for row in sorted(self._search_rows):
            super().setRowVisible(row, self.page_visible(row))
        self._search_rows.clear()

    def setRowVisible(self, row: int, visible: bool) -> None:  # noqa: N802 - Qt naming
        super().setRowVisible(row, visible)
        if not self._search_page._search_applying:
            self._page_visible[row] = bool(visible)

    # ---- direct widget toggles ----------------------------------------------
    def addRow(self, *args) -> None:  # noqa: N802 - Qt naming
        """Add a row and watch its widgets for the page hiding/showing them directly."""
        super().addRow(*args)
        self._watch_row(self.rowCount() - 1)

    def _watch_row(self, row: int) -> None:
        label, field = _row_widgets(self, row)
        if row not in self._page_visible:
            # New rows start as the page left them: explicitly hidden widgets stay hidden.
            self._page_visible[row] = not (
                (label is not None and label.isHidden())
                or (field is not None and field.isHidden())
            )
        for widget in (label, field):
            if widget is not None:
                self._owners[widget] = row
            self._watch(widget, row)
            if widget is not None:
                for child in widget.findChildren(QWidget):
                    self._watch(child, row)

    def _watch(self, widget: QWidget | None, row: int) -> None:
        if widget is None or widget in self._watchers:
            return
        widget.installEventFilter(self)
        self._watchers[widget] = row

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            shown = event.type() == QEvent.Type.Show
            # Hiding a parent sends Hide to its children too; those children are not
            # explicitly hidden, so they must not be read as the page's own wish.
            if shown != obj.isHidden() and not self._search_page._search_applying:
                # Only the row's own label/field is the page's wish. A button, note,
                # scrollbar or panel row inside it is just child text (bug: it used to
                # flip the whole row's page visibility and leak/hide the row).
                row = self._owners.get(obj)
                if row is not None:
                    self._page_visible[row] = shown
                # Either way the text a row shows changed: filter again after the
                # current event returns. The page's own calls also re-apply directly.
                self._search_page._search_reapply_soon()
        return False


class SettingsSearchCard(QFrame):
    """A card that remembers whether the page wanted it shown."""

    def __init__(self, page: SearchMixin) -> None:
        super().__init__()
        self._search_page = page
        self._page_visible: bool | None = None

    def page_visible(self) -> bool:
        # Cards start visible; the page records a hide through setVisible below.
        if self._page_visible is None:
            return True
        return self._page_visible

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt naming
        super().setVisible(visible)
        if not self._search_page._search_applying:
            self._page_visible = bool(visible)


@dataclass
class _Group:
    """One setting row plus the hint rows (and obeying labels) under it.

    Each entry is the row index and the text of that row only: a row the page hid
    must not match or count, even when a sibling row in the same group is shown.
    """

    rows: list[tuple[int, list[str]]] = field(default_factory=list)


@dataclass
class _Card:
    card: SettingsSearchCard
    title: QLabel
    subtitle: QLabel
    form: SettingsSearchForm
    extra_form: SettingsSearchForm | None = None

    def forms(self) -> list[SettingsSearchForm]:
        forms = [self.form]
        if self.extra_form is not None and not sip.isdeleted(self.extra_form):
            forms.append(self.extra_form)
        return [form for form in forms if not sip.isdeleted(form)]


def _row_widgets(form: QFormLayout, row: int) -> tuple[QWidget | None, QWidget | None]:
    label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
    field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
    if field_item is None or field_item.widget() is None:
        field_item = form.itemAt(row, QFormLayout.ItemRole.SpanningRole)
    label = label_item.widget() if label_item is not None else None
    field = field_item.widget() if field_item is not None else None
    return label, field


def _widget_texts(widget: QWidget, *, root: bool = True) -> list[str]:
    """Every piece of text ``widget`` shows (and its tooltip), skipping hidden children."""
    texts: list[str] = []

    def walk(current: QWidget, first: bool) -> None:
        if not first and current.isHidden():
            return
        tip = current.toolTip()
        if tip:
            texts.append(tip)
        if isinstance(current, QLabel):
            texts.append(current.text())
        elif isinstance(current, QAbstractButton):
            texts.append(current.text())
        elif isinstance(current, QLineEdit):
            texts.append(current.text())
            texts.append(current.placeholderText())
        elif isinstance(current, QComboBox):
            texts.append(current.currentText())
        elif isinstance(current, QPlainTextEdit):
            texts.append(current.toPlainText())
            texts.append(current.placeholderText())
        for child in current.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            walk(child, False)

    walk(widget, root)
    return [text for text in texts if text]


def _matches(texts: list[str], words: list[str]) -> bool:
    haystack = "\n".join(texts).casefold()
    return all(word in haystack for word in words)


def _query_words(query: str) -> list[str]:
    """The query's words, case-folded, each once and in order."""
    return list(dict.fromkeys(query.casefold().split()))


def _query_echo(label: QLabel, query: str) -> str:
    """The query as the no-hits line shows it: one short, plain line."""
    query = " ".join(query.split())  # line breaks and tabs would make it several lines
    if len(query) > _QUERY_ECHO_CHARS:
        query = query[:_QUERY_ECHO_CHARS] + "…"
    return QFontMetrics(label.font()).elidedText(
        query, Qt.TextElideMode.ElideRight, _QUERY_ECHO_WIDTH
    )


class SearchMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    # ---- building the field -------------------------------------------------
    def _build_search(self) -> None:
        self.search = SettingsSearchEdit()
        self.search.setMaxLength(_QUERY_MAX_CHARS)
        self.search.setPlaceholderText(i18n.tr("Search settings"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        self.search_empty = QLabel("")
        self.search_empty.setObjectName("Muted")
        self.search_empty.setTextFormat(Qt.TextFormat.PlainText)
        self.search_empty.setWordWrap(True)
        self.search_empty.setVisible(False)
        # Ctrl+F anywhere in the window while Settings is open focuses the box. The
        # window context also works before the page has focus (opened from the sidebar);
        # the visibility guard keeps it from acting on other pages.
        self._search_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self._search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._search_shortcut.activated.connect(self._focus_search)

    def _focus_search(self) -> None:
        if not self.isVisible():
            return
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    # ---- registry -----------------------------------------------------------
    def _search_add_card(
        self,
        card: SettingsSearchCard,
        title: QLabel,
        subtitle: QLabel,
        form: SettingsSearchForm,
    ) -> None:
        self._search_cards.append(_Card(card, title, subtitle, form))

    def _search_add_options_form(self, form: SettingsSearchForm) -> None:
        for info in self._search_cards:
            if info.card is getattr(self, "options_card", None):
                info.extra_form = form
                return

    def _set_widget_visible(self, widget: QWidget, visible: bool) -> None:
        """Show or hide a page widget and let an active search re-filter it.

        Called instead of ``widget.setVisible`` for the widgets the page toggles
        itself. Toggling a whole row (its label or field) also records the page's
        wish; toggling a child only needs the filter to run again, because the
        row may now show different text.
        """
        for info in self._search_cards:
            for form in info.forms():
                row = form.owner_row(widget)
                if row is not None:
                    form.set_page_visible(row, visible)
                    break
        widget.setVisible(visible)
        self._search_reapply()

    # ---- filtering ----------------------------------------------------------
    def _search_changed(self, _text: str) -> None:
        self._search_apply()

    def _search_reapply(self) -> None:
        """Apply the current query again (the form or a row changed)."""
        if sip.isdeleted(self):
            return
        if self._search_active and not self._search_loading:
            self._search_apply()

    def _search_reapply_soon(self) -> None:
        """Re-apply after the current event returns (a widget was shown/hidden)."""
        if self._search_active and not self._search_loading:
            QTimer.singleShot(0, self._search_reapply)

    def _search_begin_load(self) -> None:
        """Stop filtering while ``load`` rebuilds the form; apply once at the end."""
        self._search_loading = True

    def _search_end_load(self) -> None:
        """The tab a clear returns to is the one the profile was loaded on."""
        self._search_loading = False
        self._search_tab = self.tabs.currentIndex()
        self._search_reapply()

    def _search_options_toggled(self, _checked: bool) -> None:
        """Show advanced options changed the rows; keep the active query applied."""
        self._search_reapply()

    def _search_apply(self) -> None:
        if self._search_applying:
            return
        self._search_applying = True
        try:
            words = _query_words(self.search.text())
            if not words:
                self._search_reset()
                return
            if not self._search_active:
                self._search_active = True
                self._search_titles = [
                    self.tabs.tabText(index) for index in range(self.tabs.count())
                ]
                self._search_tab = self.tabs.currentIndex()
            self._search_restore()
            counts = [0] * self.tabs.count()
            for info in self._search_cards:
                self._search_card(info, words, counts)
            total = sum(counts)
            if total == 0:
                self._search_restore_titles()
                query = self.search.text().strip()
                self.search_empty.setText(
                    i18n.tr(
                        "Nothing matches “{query}”",
                        query=_query_echo(self.search_empty, query),
                    )
                )
                self.search_empty.setVisible(True)
                return
            self.search_empty.setVisible(False)
            self._search_restore_counts(counts)
        finally:
            self._search_applying = False

    def _search_card(self, info: _Card, words: list[str], counts: list[int]) -> None:
        card = info.card
        if sip.isdeleted(card) or not card.page_visible():
            return
        tab = self._search_tab_of(card)
        if tab < 0 or not self.tabs.isTabVisible(tab):
            # A hidden tab (Nadare-only Settings) is neither searched nor counted,
            # so it cannot hide the "Nothing matches" line.
            return
        card_hit = _matches([info.title.text(), info.subtitle.text()], words)
        groups = 0
        visible = False
        for form in info.forms():
            extra = form is info.extra_form
            any_option = False
            for group in self._search_groups(form):
                shown = [
                    (row, texts)
                    for row, texts in group.rows
                    if texts and self._search_row_visible(form, row)
                ]
                # A setting and its hint are one result: the words may be split
                # between the row and the hint label under it.
                hit = card_hit or _matches(
                    [text for _row, texts in shown for text in texts], words
                )
                if hit and shown:
                    visible = True
                    groups += 1
                    any_option = any_option or extra
                    continue
                for row, _texts in group.rows:
                    if self._search_row_visible(form, row):
                        form.hide_for_search(row)
            if extra and not any_option:
                self._search_hide_container(info, form)
        if not visible:
            self._search_hidden_cards.add(card)
            card.setVisible(False)
            return
        counts[tab] += groups

    def _search_groups(self, form: SettingsSearchForm) -> list[_Group]:
        groups: list[_Group] = []
        current: _Group | None = None
        for row in range(form.rowCount()):
            label, field = _row_widgets(form, row)
            if field is None or field is getattr(self, "_options_host", None):
                continue
            texts = _widget_texts(label) if label is not None else []
            texts += _widget_texts(field)
            label_text = label.text() if isinstance(label, QLabel) else ""
            hint = (
                not label_text and isinstance(field, QLabel) and field.objectName() in _HINT_STYLES
            )
            if hint and current is not None:
                current.rows.append((row, texts))
                continue
            current = _Group(rows=[(row, texts)])
            groups.append(current)
        return groups

    def _search_row_visible(self, form: SettingsSearchForm, row: int) -> bool:
        if not form.isRowVisible(row) or not form.page_visible(row):
            return False
        label, field = _row_widgets(form, row)
        return not (
            (label is not None and label.isHidden()) or (field is not None and field.isHidden())
        )

    def _search_hide_container(self, info: _Card, extra: SettingsSearchForm) -> None:
        """Hide the options area itself when none of its rows stayed visible."""
        host = getattr(self, "_options_host", None)
        if host is None:
            return
        for row in range(info.form.rowCount()):
            _label, field = _row_widgets(info.form, row)
            if field is host:
                info.form.hide_for_search(row)
                return

    def _search_tab_of(self, widget: QWidget) -> int:
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            if page is not None and page.isAncestorOf(widget):
                return index
        return -1

    def _search_restore(self) -> None:
        for info in self._search_cards:
            for form in info.forms():
                form.restore_search_rows()
        for card in list(self._search_hidden_cards):
            if not sip.isdeleted(card):
                card.setVisible(card.page_visible())
        self._search_hidden_cards.clear()

    def _search_restore_titles(self) -> None:
        if self._search_titles is None:
            return
        for index, title in enumerate(self._search_titles):
            if index < self.tabs.count():
                self.tabs.setTabText(index, title)

    def _search_restore_counts(self, counts: list[int]) -> None:
        self._search_restore_titles()
        if self._search_titles is None:
            return
        for index, count in enumerate(counts):
            if index < self.tabs.count():
                if self.tabs.isTabVisible(index):
                    self.tabs.setTabText(index, f"{self._search_titles[index]} ({count})")
                else:
                    # A tab the page hid gets no count: it is not part of the search.
                    self.tabs.setTabText(index, self._search_titles[index])
        current = self.tabs.currentIndex()
        if 0 <= current < len(counts) and counts[current] == 0:
            for index, count in enumerate(counts):
                if count and self.tabs.isTabVisible(index):
                    self.tabs.setCurrentIndex(index)
                    break

    def _search_reset(self) -> None:
        self._search_restore()
        self._search_restore_titles()
        if (
            self._search_active
            and 0 <= self._search_tab < self.tabs.count()
            and self.tabs.isTabVisible(self._search_tab)
            and self.tabs.currentIndex() != self._search_tab
        ):
            self.tabs.setCurrentIndex(self._search_tab)
        self._search_active = False
        self._search_titles = None
        self.search_empty.setText("")
        self.search_empty.setVisible(False)


__all__ = [
    "SearchMixin",
    "SettingsSearchCard",
    "SettingsSearchEdit",
    "SettingsSearchForm",
]
