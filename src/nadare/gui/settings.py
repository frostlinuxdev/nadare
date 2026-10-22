# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Settings page: runner picker, game folder, language, update check, background.

All values are persisted through :class:`~nadare.core.config.Config`, never by
hand. Runner enumeration prefers ``core.runners`` and degrades gracefully to a
scan of the Nadare runners directory when that module is unavailable.
"""

from __future__ import annotations

import copy
import shlex
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core import runners as runners_core
from ..core.backends import BackendError, Option, kinds, supports_remember_me
from ..core.backends import create as create_backend
from ..core.backends import get as backend_get
from ..core.config import Config
from . import i18n
from .library import profile_title
from .workers import CallTask

#: Label used for the "no runner" choice.
SYSTEM_WINE = "System Wine"

#: ``(min, max)`` for ``int`` options that need a tighter bound than the default.
INT_RANGES: dict[str, tuple[int, int]] = {"download_workers": (1, 16)}
DEFAULT_INT_RANGE = (0, 999999)

#: Old beta keys mapped to the canonical field that now owns their value.
LEGACY_PROFILE_KEYS = {"runner": "proton_runner", "prefix": "wineprefix"}

#: Native language names for the picker; an unknown code shows as-is.
LANGUAGE_NAMES = {"en": "English"}


def _runner_label(path: Path) -> str:
    """Picker label for an installed runner: tested builds are marked."""
    if runners_core.is_recommended(path):
        return f"{path.name} · recommended"
    if runners_core.runner_family(path):
        return f"{path.name} · untested"
    return path.name


def discover_runners(kind: str = "") -> list[tuple[str, str]]:
    """Return ``(label, value)`` pairs for the runner picker.

    Value is a filesystem path for an installed Proton build. The empty value is
    the backend's default: the recommended (tested) DW-Proton for the games that
    run through Proton, system Wine for the generic backends. The games that run
    through Proton do not offer system Wine as a separate choice.
    """
    entries: list[tuple[str, str]] = []
    proton_default = kind in runners_core.PROTON_DEFAULT_BACKENDS
    if proton_default:
        tag = runners_core.pinned_tag(runners_core.DEFAULT_FAMILY) or "DW-Proton"
        entries.append((f"Default · recommended ({tag})", ""))
    seen: set[str] = set()
    for path in runners_core.all_runners():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entries.append((_runner_label(path), key))
    if not proton_default:
        entries.append((SYSTEM_WINE, ""))
    return entries


def _env_to_text(value: Any) -> str:
    """Render an env mapping as sorted ``KEY=VALUE`` lines."""
    if not isinstance(value, dict):
        return ""
    return "\n".join(f"{key}={val}" for key, val in sorted(value.items()))


def _text_to_env(text: str, warnings: list[str] | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a mapping (blank lines ignored).

    A non-empty line without an ``=`` is reported through ``warnings`` instead
    of disappearing silently; the valid lines are still returned.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key:
            out[key] = value.strip()
        elif warnings is not None:
            warnings.append(
                f"Environment: '{line}' is not KEY=VALUE, so it was not saved. "
                "Add an = sign or remove the line."
            )
    return out



def _clear_layout(layout) -> None:  # noqa: ANN001 - QLayout
    """Remove and schedule deletion of everything in ``layout``, recursively.

    ``takeAt(0).widget()`` is ``None`` for a nested layout, so deleting only
    top-level widgets left the previous options form (and every editor in it)
    alive and visible (#47).
    """
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            break
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()

class SettingsPage(QWidget):
    """Editable settings for one profile plus the shared language/update options."""

    saved = pyqtSignal(str)  # profile name
    cancelled = pyqtSignal()
    remove_requested = pyqtSignal(str)  # profile name
    remake_prefix_requested = pyqtSignal(str)  # profile name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._config: Config | None = None
        self._profile = ""
        # Every backend option key maps to its editor widget. "install_dir" and
        # "runner" live in the Game files card; the rest are built per backend.
        self._option_widgets: dict[str, QWidget] = {}
        self._option_free: dict[str, QLabel] = {}
        self._option_browse: dict[str, QPushButton] = {}
        self._option_rows: list[tuple[bool, int]] = []
        self._options_form: QFormLayout | None = None
        self._files_form: QFormLayout | None = None
        self._install_dir_row = -1
        self._prefix_lock_note: QLabel | None = None
        self._fetch_button: QPushButton | None = None
        self._pending_art: dict[str, str] = {}
        self._loaded_state: dict[str, Any] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 28)
        root.setSpacing(16)

        header = QHBoxLayout()
        self.back = QPushButton("←  Back")
        self.back.setObjectName("GhostButton")
        self.back.clicked.connect(self._request_back)
        header.addWidget(self.back)
        header.addStretch(1)
        root.addLayout(header)

        intro = QVBoxLayout()
        intro.setSpacing(6)
        kicker = QLabel("CONFIGURATION")
        kicker.setObjectName("Section")
        intro.addWidget(kicker)
        self.heading = QLabel("Settings")
        self.heading.setObjectName("Title")
        intro.addWidget(self.heading)
        subtitle = QLabel("How this profile is installed, launched and displayed.")
        subtitle.setObjectName("Subtitle")
        intro.addWidget(subtitle)
        root.addLayout(intro)

        # Scroll so the grouped cards never clip on short displays.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("SettingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 2, 2)
        content_layout.setSpacing(16)

        # ---- profile --------------------------------------------------------
        profile_card, profile_form = self._section(
            "Profile", "Basic identity. The backend is fixed when the game is added."
        )
        self.profile_name = QLineEdit()
        self.profile_name.setReadOnly(True)
        self.title = QLineEdit()
        self.title.setPlaceholderText("name shown in the library")
        # The backend is chosen once, in Add game. Switching it afterwards would
        # point the game's existing prefix and files at the wrong driver.
        self._backend_kind = ""
        profile_form.addRow("Profile ID", self.profile_name)
        profile_form.addRow("Title", self.title)
        content_layout.addWidget(profile_card)

        # ---- account --------------------------------------------------------
        account_card, account_form = self._section(
            "Account", "Whether this device remembers the STOVE login for this game."
        )
        self.remember = QCheckBox("Remember me on this device")
        self.remember_hint = QLabel(
            "Stores the login in your system keyring. Off by default; turning it off "
            "removes the saved login when you press Save."
        )
        self.remember_hint.setObjectName("Subtitle")
        self.remember_hint.setWordWrap(True)
        account_form.addRow("", self.remember)
        account_form.addRow("", self.remember_hint)
        self.account_card = account_card
        content_layout.addWidget(account_card)

        # ---- game files -----------------------------------------------------
        files_card, files_form = self._section(
            "Game files", "Where the game lives and which compatibility layer runs it."
        )
        self.install_dir = QLineEdit()
        self.install_dir.editingFinished.connect(self._update_free_hints)
        self.runner = QComboBox()
        self.runner.setEditable(True)
        self.runner.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        files_form.addRow("Game folder", self._with_browse(self.install_dir, "dir"))
        self._files_form = files_form
        self._install_dir_row = files_form.rowCount() - 1
        self.install_dir_free = QLabel("")
        self.install_dir_free.setObjectName("Muted")
        files_form.addRow("", self.install_dir_free)
        runner_row = QWidget()
        runner_layout = QHBoxLayout(runner_row)
        runner_layout.setContentsMargins(0, 0, 0, 0)
        runner_layout.addWidget(self.runner, 1)
        manage = QPushButton("Manage runners…")
        manage.setObjectName("GhostButton")
        manage.setToolTip("See the Proton runners Nadare downloaded and delete unused ones.")
        manage.clicked.connect(self._open_runners_dialog)
        runner_layout.addWidget(manage)
        self.manage_runners = manage
        files_form.addRow("Runner", runner_row)
        self._runner_row = files_form.rowCount() - 1
        self.remake_prefix_btn = QPushButton("Rebuild Wine prefix…")
        self.remake_prefix_btn.setObjectName("GhostButton")
        self.remake_prefix_btn.setToolTip(
            "Move the current Wine prefix aside and create a fresh one. Your game "
            "files are kept. The old prefix is not deleted."
        )
        self.remake_prefix_btn.clicked.connect(
            lambda: self.remake_prefix_requested.emit(self._profile)
        )
        files_form.addRow("Wine prefix", self.remake_prefix_btn)
        self._remake_prefix_row = files_form.rowCount() - 1
        content_layout.addWidget(files_card)

        # ---- game options ---------------------------------------------------
        options_card, options_form = self._section(
            "Game options",
            "Backend-specific settings. Text fields can be left empty to use the default; "
            "use Restore defaults to reset the rest.",
        )
        self.show_advanced = QCheckBox("Show advanced options")
        self.show_advanced.toggled.connect(self._apply_advanced)
        options_form.addRow("", self.show_advanced)
        self.options_note = QLabel("")
        self.options_note.setObjectName("Muted")
        self.options_note.setWordWrap(True)
        options_form.addRow("", self.options_note)
        self.restore_defaults = QPushButton("Restore defaults")
        self.restore_defaults.setObjectName("GhostButton")
        self.restore_defaults.setToolTip(
            "Put the options in this section back to their defaults. "
            "Nothing is saved until you press Save changes."
        )
        self.restore_defaults.setVisible(False)
        self.restore_defaults.clicked.connect(self._restore_option_defaults)
        options_form.addRow("", self.restore_defaults)
        self._options_host = QWidget()
        self._options_host_layout = QVBoxLayout(self._options_host)
        self._options_host_layout.setContentsMargins(0, 0, 0, 0)
        self._options_host_layout.setSpacing(0)
        options_form.addRow(self._options_host)
        self.options_card = options_card
        content_layout.addWidget(options_card)

        # ---- appearance -----------------------------------------------------
        appearance_card, appearance_form = self._section(
            "Appearance", "Optional key art for this game's library card."
        )
        self.background = QLineEdit()
        self.background.setPlaceholderText("optional image for this game's card")
        appearance_form.addRow(
            "Custom background",
            self._with_browse(self.background, "image", clear=True, fetch=True),
        )
        content_layout.addWidget(appearance_card)

        # ---- launcher -------------------------------------------------------
        launcher_card, launcher_form = self._section(
            "Launcher", "Shared options for the whole installation."
        )
        self.language = QComboBox()
        for code in i18n.available():
            self.language.addItem(LANGUAGE_NAMES.get(code, code), code)
        self.language_label = QLabel("Language")
        self.check_updates = QCheckBox("Check for launcher and game updates")
        launcher_form.addRow(self.language_label, self.language)
        launcher_form.addRow("", self.check_updates)
        # With a single shipped locale the picker is meaningless: hide the row
        # until a second catalogue makes it actionable.
        multiple_languages = len(i18n.available()) > 1
        self.language_label.setVisible(multiple_languages)
        self.language.setVisible(multiple_languages)
        content_layout.addWidget(launcher_card)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self.remove_button = QPushButton("Remove from library…")
        self.remove_button.setObjectName("GhostButton")
        self.remove_button.setToolTip(
            "Remove this game from Nadare. Game files are kept by default."
        )
        self.remove_button.clicked.connect(self._request_remove)
        footer.addWidget(self.remove_button)
        self.note = QLabel("")
        self.note.setObjectName("Muted")
        footer.addWidget(self.note, 1)
        save = QPushButton("Save changes")
        save.setObjectName("Primary")
        save.setMinimumHeight(44)
        save.clicked.connect(self.save)
        footer.addWidget(save)
        root.addLayout(footer)

    def _request_back(self) -> None:
        """Leave Settings, asking first when the form has unsaved edits."""
        if self._has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                "Discard changes",
                "Discard your changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.cancelled.emit()

    def _form_state(self) -> dict[str, Any]:
        """The form's current values, for comparing against the loaded ones."""
        state: dict[str, Any] = {
            "title": self.title.text(),
            "background": self.background.text(),
            "remember": self.remember.isChecked(),
            "language": self.language.currentData(),
            "check_updates": self.check_updates.isChecked(),
        }
        for key, widget in self._option_widgets.items():
            if key == "runner":
                state[key] = self._runner_value()
            elif isinstance(widget, QPlainTextEdit):
                state[key] = widget.toPlainText()
            elif isinstance(widget, QLineEdit):
                state[key] = widget.text()
            elif isinstance(widget, QCheckBox):
                state[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                state[key] = widget.value()
            elif isinstance(widget, QComboBox):
                state[key] = widget.currentData()
        return state

    def _has_unsaved_changes(self) -> bool:
        return self._loaded_state is not None and self._loaded_state != self._form_state()

    def _request_remove(self) -> None:
        if self._profile:
            self.remove_requested.emit(self._profile)

    # ---- helpers -----------------------------------------------------------
    def _section(self, title: str, subtitle: str) -> tuple[QFrame, QFormLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(14)

        head = QVBoxLayout()
        head.setSpacing(4)
        heading = QLabel(title.upper())
        heading.setObjectName("Section")
        head.addWidget(heading)
        hint = QLabel(subtitle)
        hint.setObjectName("Subtitle")
        head.addWidget(hint)
        outer.addLayout(head)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        outer.addLayout(form)
        return frame, form

    def _with_browse(
        self, line_edit: QLineEdit, kind: str, *, clear: bool = False, fetch: bool = False
    ) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(line_edit, 1)
        browse = QPushButton("Browse")
        browse.setObjectName("GhostButton")
        browse.clicked.connect(lambda: self._browse(line_edit, kind))
        layout.addWidget(browse)
        if fetch:
            fetch_button = QPushButton("Fetch art")
            fetch_button.setObjectName("GhostButton")
            fetch_button.clicked.connect(self._fetch_art)
            layout.addWidget(fetch_button)
            self._fetch_button = fetch_button
        if clear:
            clear_button = QPushButton("Clear")
            clear_button.setObjectName("GhostButton")
            clear_button.clicked.connect(line_edit.clear)
            layout.addWidget(clear_button)
        return row

    def _fetch_art(self) -> None:
        """Download cover art for the current profile off the GUI thread.

        ``artwork.fetch_all`` performs several blocking HTTPS queries; running it
        here would freeze the window for tens of seconds. The live profile is
        never handed to the worker (it records provenance, which would race a
        concurrent ``Config.save()``): it gets a deep copy and the result is
        applied on the GUI thread.
        """
        if self._config is None or not self._profile:
            return
        from ..core import artwork

        try:
            profile = self._config.profile(self._profile)
        except KeyError:
            return
        title = self.title.text().strip() or self._profile
        with self._config.locked():
            snapshot = copy.deepcopy(profile)
        profile_name = self._profile
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(False)
        self.note.setText("Searching for artwork…")

        task = CallTask(lambda: artwork.fetch_all(snapshot, title=title))
        task.signals.done.connect(
            lambda found: self._on_art_fetched(profile_name, found)
        )
        task.signals.failed.connect(self._on_art_failed)
        QThreadPool.globalInstance().start(task)

    def _on_art_fetched(self, profile_name: str, found: object) -> None:
        """Apply a finished art fetch on the GUI thread."""
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(True)
        if self._config is None or profile_name != self._profile:
            return  # the user moved on while the fetch was in flight
        paths = found if isinstance(found, dict) else {}
        if not paths:
            self.note.setText("No artwork found")
            return

        self._pending_art = dict(paths)
        cover = paths.get("cover") or next(iter(paths.values()))
        self.background.setText(str(cover))
        self.note.setText(f"Fetched {len(paths)} image(s) to {Path(cover).parent}")

    def _on_art_failed(self, message: str) -> None:
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(True)
        self.note.setText(f"Artwork fetch failed: {message}")

    def _browse(self, line_edit: QLineEdit, kind: str) -> None:
        start = line_edit.text().strip() or str(Path.home())
        if kind == "dir":
            chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        elif kind == "file":
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Select file", start, "All files (*)"
            )
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self,
                "Select background image",
                start,
                "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)",
            )
        if chosen:
            line_edit.setText(chosen)

    def _runner_value(self) -> str:
        """The runner the form currently selects (a listed entry, or typed text)."""
        text = self.runner.currentText().strip()
        index = self.runner.currentIndex()
        if index >= 0 and text == self.runner.itemText(index):
            return str(self.runner.currentData() or "")
        return text

    def _open_runners_dialog(self) -> None:
        """Show the runner manager, then refresh the picker (a runner may be gone)."""
        from .runners_dialog import ManageRunnersDialog

        # Judge "in use" by what the form shows now, not only what is saved: an
        # unsaved selection must not make the runner look unused.
        profiles = copy.deepcopy(self._config.profiles) if self._config is not None else {}
        if self._profile in profiles:
            profiles[self._profile]["runner"] = self._runner_value()
        dialog = ManageRunnersDialog(profiles, self)
        dialog.exec()
        # The default entry's data is ""; falling back to its label would persist the label.
        self._reload_runners(self._runner_value())

    def _reload_runners(self, selected: str) -> bool:
        """Refill the picker and select ``selected``; False when it is not a listed runner."""
        self.runner.clear()
        for label, value in discover_runners(self._backend_kind):
            self.runner.addItem(label, value)
        index = self.runner.findData(selected) if selected else -1
        matched = index >= 0
        if index < 0 and selected in runners_core.RUNNERS:
            # A family id (``"dwproton"``) means that family's pinned, tested build.
            tag = runners_core.pinned_tag(selected) or selected
            self.runner.insertItem(0, f"{tag} · recommended (pinned)", selected)
            index = 0
            matched = True
        elif index < 0 and selected in ("latest", "auto"):
            # resolve_runner() understands these aliases; they are not broken.
            self.runner.insertItem(0, f"{selected.title()} installed runner", selected)
            index = 0
            matched = True
        elif index < 0 and selected:
            self.runner.insertItem(0, f"Missing · {selected}", selected)
            index = 0
        if index < 0:
            index = self.runner.findData("")
            matched = True
        self.runner.setCurrentIndex(max(0, index))
        return matched

    # ---- backend options ---------------------------------------------------
    @staticmethod
    def _backend_options(kind: str) -> tuple[Option, ...]:
        """The declared options for ``kind`` (empty for unknown backends)."""
        try:
            return backend_get(kind).options
        except Exception:  # noqa: BLE001 - an unknown backend simply has none
            return ()

    def option_widget(self, key: str) -> QWidget | None:
        """The editor widget for an option key, or ``None`` when not shown."""
        return self._option_widgets.get(key)

    def _build_options(self, kind: str) -> None:
        """(Re)build the Game options card for ``kind``."""
        _clear_layout(self._options_host_layout)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        self._options_host_layout.addLayout(form)
        self._options_form = form
        self._option_rows = []
        self._option_free = {}
        self._option_browse = {}
        self._prefix_lock_note = None
        options = self._backend_options(kind)
        self.restore_defaults.setVisible(bool(options))
        has_install_dir = any(option.key == "install_dir" for option in options)
        # install_dir / runner are always edited in the Game files card.
        self._option_widgets = {"install_dir": self.install_dir, "runner": self.runner}
        if self._files_form is not None:
            self._files_form.setRowVisible(self._install_dir_row, has_install_dir)
            uses_runner = kind in runners_core.PROTON_DEFAULT_BACKENDS or kind == "wine"
            self._files_form.setRowVisible(self._runner_row, uses_runner)
        if not has_install_dir:
            # Steam/CZN/FGO never read this key; an old value must not look live.
            self.install_dir.clear()
        for option in options:
            if option.key in ("install_dir", "runner"):
                continue
            row_widget, editor = self._create_editor(option)
            self._option_widgets[option.key] = editor
            text = option.label
            if option.key == "prefix" and option.label == "Wine prefix" and not has_install_dir:
                text = "Game location (Wine prefix)"
            label = QLabel(text)
            if option.help:
                label.setToolTip(option.help)
                row_widget.setToolTip(option.help)
            if option.help and not option.advanced:
                row_widget = self._with_help(option, row_widget)
            form.addRow(label, row_widget)
            self._option_rows.append((option.advanced, form.rowCount() - 1))
        self._apply_advanced(self.show_advanced.isChecked())

    def _restore_option_defaults(self) -> None:
        """Put every backend option editor back to its default (without saving)."""
        for option in self._backend_options(self._backend_kind):
            if option.key in ("install_dir", "runner"):
                continue
            editor = self._option_widgets.get(option.key)
            if editor is None:
                continue
            kind = option.kind
            if kind == "choice":
                index = editor.findData(option.default)
                editor.setCurrentIndex(index if index >= 0 else 0)
            elif kind == "bool":
                editor.setChecked(bool(option.default))
            elif kind == "int":
                editor.setValue(
                    option.default
                    if isinstance(option.default, int)
                    else editor.minimum()
                )
            elif kind == "env":
                editor.setPlainText("")
            elif kind in ("text", "args", "path_dir", "path_file"):
                editor.setText("")
            elif isinstance(editor, QLineEdit):
                editor.setText("")
        self._update_free_hints()

    def _with_help(self, option: Option, row_widget: QWidget) -> QWidget:
        """Put the option's help (and any install lock note) under its field."""
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(row_widget)
        hint = QLabel(option.help)
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        if option.key == "prefix":
            self._prefix_lock_note = QLabel(
                "The game is installed, so this location is locked. Moving it would make "
                "the game unfindable; remove the game and add it again to move it."
            )
            self._prefix_lock_note.setObjectName("Muted")
            self._prefix_lock_note.setWordWrap(True)
            self._prefix_lock_note.setVisible(False)
            layout.addWidget(self._prefix_lock_note)
        return wrapper

    def _create_editor(self, option: Option) -> tuple[QWidget, QWidget]:
        """Return ``(row_widget, editor)`` for one option."""
        kind = option.kind
        if kind in ("text", "args"):
            editor = QLineEdit()
            return editor, editor
        if kind == "env":
            editor = QPlainTextEdit()
            editor.setPlaceholderText("KEY=VALUE")
            editor.setMaximumHeight(90)
            return editor, editor
        if kind in ("path_dir", "path_file"):
            editor = QLineEdit()
            row = self._with_browse(editor, "dir" if kind == "path_dir" else "file")
            browse = row.findChild(QPushButton)
            if browse is not None:
                self._option_browse[option.key] = browse
            if kind != "path_dir":
                return row, editor
            hint = QLabel("")
            hint.setObjectName("Muted")
            self._option_free[option.key] = hint
            editor.editingFinished.connect(self._update_free_hints)
            wrapper = QWidget()
            layout = QVBoxLayout(wrapper)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            layout.addWidget(row)
            layout.addWidget(hint)
            return wrapper, editor
        if kind == "int":
            editor = QSpinBox()
            editor.setRange(*INT_RANGES.get(option.key, DEFAULT_INT_RANGE))
            return editor, editor
        if kind == "bool":
            editor = QCheckBox()
            return editor, editor
        if kind == "choice":
            editor = QComboBox()
            for label, value in option.choices or ():
                editor.addItem(label, value)
            return editor, editor
        if kind == "runner":
            return self.runner, self.runner
        editor = QLineEdit()
        return editor, editor

    def _game_is_installed(self) -> bool:
        """Whether this profile's game is already on disk (read-only check)."""
        if self._config is None or not self._profile:
            return False
        try:
            status = create_backend(self._profile, self._config).status()
        except Exception:  # noqa: BLE001 - a probe failure must not block Settings
            return False
        return status.state in ("installed", "running")

    def _set_prefix_locked(self, locked: bool) -> None:
        """Stop an installed game's prefix from being silently repointed."""
        editor = self._option_widgets.get("prefix")
        if isinstance(editor, QLineEdit):
            editor.setReadOnly(locked)
        browse = self._option_browse.get("prefix")
        if browse is not None:
            browse.setEnabled(not locked)
        if self._prefix_lock_note is not None:
            self._prefix_lock_note.setVisible(locked)

    def _apply_advanced(self, show: bool) -> None:
        if self._options_form is None:
            return
        for advanced, row in self._option_rows:
            self._options_form.setRowVisible(row, bool(show) or not advanced)

    def _free_hint(self, path: str) -> str:
        text = path.strip()
        if not text:
            return ""
        try:
            from ..core import diskspace

            free = diskspace.human_size(diskspace.free_bytes(text))
            return f"{free} free on {diskspace.mount_point(text)}"
        except (OSError, BackendError):
            return ""

    def _update_free_hints(self) -> None:
        self.install_dir_free.setText(self._free_hint(self.install_dir.text()))
        for key, label in self._option_free.items():
            editor = self._option_widgets.get(key)
            if isinstance(editor, QLineEdit):
                label.setText(self._free_hint(editor.text()))

    @staticmethod
    def _option_value(data: dict[str, Any], key: str) -> Any:
        """Read ``key`` from the profile's top level or its ``extra`` mapping.

        Older betas wrote ``proton_runner``/``wineprefix``; show that value when
        the canonical field is empty so Settings does not look blank.
        """
        value = data.get(key)
        if value in (None, ""):
            extra = data.get("extra")
            if isinstance(extra, dict):
                value = extra.get(key)
        if value in (None, ""):
            old = LEGACY_PROFILE_KEYS.get(key)
            if old:
                value = data.get(old)
                if value in (None, ""):
                    extra = data.get("extra")
                    if isinstance(extra, dict):
                        value = extra.get(old)
        return value

    def _set_editor(self, option: Option, editor: QWidget, value: Any) -> None:
        kind = option.kind
        if kind in ("text", "args", "path_dir", "path_file"):
            editor.setText("" if value in (None, "") else str(value))
        elif kind == "env":
            editor.setPlainText(_env_to_text(value))
        elif kind == "int":
            try:
                editor.setValue(int(value))
            except (TypeError, ValueError):
                editor.setValue(
                    int(option.default) if option.default is not None else editor.minimum()
                )
        elif kind == "bool":
            # An unset key means "use the option's default", not "off".
            editor.setChecked(bool(option.default) if value is None else bool(value))
        elif kind == "choice":
            index = editor.findData(value)
            if index < 0 and value not in (None, ""):
                index = editor.findText(str(value))
            editor.setCurrentIndex(index if index >= 0 else 0)
        elif kind == "runner":
            text = str(value or "")
            if not self._reload_runners(text):
                self.runner.setEditText(text)

    def _load_options(self, kind: str, data: dict[str, Any]) -> None:
        self.show_advanced.setChecked(False)
        self.options_note.setText("")
        self._build_options(kind)
        for option in self._backend_options(kind):
            editor = self._option_widgets.get(option.key)
            if editor is not None:
                self._set_editor(option, editor, self._option_value(data, option.key))

    def _read_editor(self, option: Option, editor: QWidget, warnings: list[str]) -> Any:
        kind = option.kind
        if kind in ("text", "args"):
            value = editor.text().strip()
            if kind == "args" and value:
                try:
                    shlex.split(value)
                except ValueError:
                    warnings.append(
                        f"{option.label}: has an opening quote with no closing quote, so the "
                        "game will fail to start. Fix it and save again."
                    )
            return value
        if kind == "env":
            return _text_to_env(editor.toPlainText(), warnings)
        if kind in ("path_dir", "path_file"):
            value = editor.text().strip()
            if value:
                path = Path(value).expanduser()
                if kind == "path_dir" and not path.is_dir():
                    warnings.append(f"{option.label}: folder does not exist (kept anyway).")
                elif kind == "path_file" and not path.is_file():
                    warnings.append(f"{option.label}: file does not exist (kept anyway).")
            return value
        if kind == "int":
            return editor.value()
        if kind == "bool":
            return editor.isChecked()
        if kind == "choice":
            return editor.currentData()
        if kind == "runner":
            text = editor.currentText().strip()
            index = editor.currentIndex()
            if index >= 0 and text == editor.itemText(index):
                return editor.currentData()
            return text
        return None

    @staticmethod
    def _is_default_option_value(option: Option, value: Any) -> bool:
        """Whether ``value`` means "use the backend default" for ``option``."""
        if option.kind in ("text", "args", "path_dir", "path_file", "env"):
            if value in (None, "", {}):
                return True
        return value == option.default

    @staticmethod
    def _store(data: dict[str, Any], key: str, value: Any) -> None:
        """Write ``key`` at the top level, or remove it when the value is empty."""
        if value is None or value == "" or value == {} or value == []:
            data.pop(key, None)
            extra = data.get("extra")
            if isinstance(extra, dict):
                extra.pop(key, None)
        else:
            data[key] = value
        old = LEGACY_PROFILE_KEYS.get(key)
        if old:
            # The canonical field now owns this value, so drop the old alias
            # before it can shadow what the user just chose.
            data.pop(old, None)
            extra = data.get("extra")
            if isinstance(extra, dict):
                extra.pop(old, None)

    # ---- load / save -------------------------------------------------------
    def load(self, config: Config, profile: str) -> None:
        """Populate the form from ``config`` for ``profile``."""
        self._config = config
        self._profile = profile
        self._pending_art = {}
        data = config.profile(profile)

        self.profile_name.setText(profile)
        self.heading.setText(f"Settings — {profile_title(profile, data)}")
        self.title.setText(str(data.get("title") or ""))
        kind = str(data.get("backend") or "")
        self._backend_kind = kind
        self.install_dir.setText(str(data.get("install_dir") or ""))
        self._update_free_hints()
        runner_text = str(data.get("runner") or "")
        if not self._reload_runners(runner_text):
            self.runner.setEditText(runner_text)
        self.background.setText(str(data.get("custom_background") or ""))
        self._load_options(kind, data)
        self._update_free_hints()
        if self._files_form is not None:
            self._files_form.setRowVisible(
                self._remake_prefix_row, kind in runners_core.PROTON_DEFAULT_BACKENDS
            )
        has_wine_prefix = any(
            option.key == "prefix" and option.label == "Wine prefix"
            for option in self._backend_options(kind)
        )
        self._set_prefix_locked(has_wine_prefix and self._game_is_installed())

        supported = supports_remember_me(str(data.get("backend") or ""))
        self.account_card.setVisible(supported)
        self.remember.setChecked(bool(data.get("remember_me", False)))

        language = str(config.data.get("language") or i18n.DEFAULT_LANGUAGE)
        lang_index = self.language.findData(language)
        self.language.setCurrentIndex(lang_index if lang_index >= 0 else 0)
        self.check_updates.setChecked(bool(config.data.get("check_updates", True)))
        self.note.setText(f"Saved in {config.path}")
        self._loaded_state = self._form_state()

    def save(self) -> None:
        if self._config is None or not self._profile:
            self.cancelled.emit()
            return
        config = self._config
        try:
            data = config.profile(self._profile)
        except KeyError:
            self.cancelled.emit()
            return

        backend = self._backend_kind
        if backend not in kinds():
            QMessageBox.critical(
                self,
                "Settings",
                f"{backend!r} is not a backend this build knows, so this game's "
                "settings cannot be saved.",
            )
            return

        runner_text = self.runner.currentText().strip()
        index = self.runner.currentIndex()
        if index >= 0 and runner_text == self.runner.itemText(index):
            runner_value = self.runner.currentData()
        else:
            # The user typed a custom runner path/tag.
            runner_value = runner_text

        # The mutation and the write are taken under the config lock (as the
        # worker-side ``BackendContext.set`` does); otherwise a worker save can
        # serialise this profile dict while it is being changed.
        import copy as _copy

        with config.locked():
            backup = (
                _copy.deepcopy(data),
                config.data.get("language"),
                config.data.get("check_updates"),
            )
        warnings: list[str] = []
        custom_runner = not (
            index >= 0 and runner_text == self.runner.itemText(index)
        )
        if custom_runner and runner_text:
            try:
                runners_core.resolve_runner(runner_text)
            except runners_core.RunnerError:
                warnings.append(
                    "Runner: that runner is not installed. Pick one from the list, or "
                    "leave the box empty to use the default."
                )
        install_dir_option = any(
            option.key == "install_dir" for option in self._backend_options(backend)
        )
        try:
            with config.locked():
                data["title"] = self.title.text().strip()
                data["backend"] = backend
                if install_dir_option:
                    self._store(data, "install_dir", self.install_dir.text().strip())
                self._store(data, "runner", str(runner_value or ""))
                data["custom_background"] = self.background.text().strip()

                for option in self._backend_options(data["backend"]):
                    if option.key in ("install_dir", "runner"):
                        continue
                    editor = self._option_widgets.get(option.key)
                    if editor is None:
                        continue
                    existing = self._option_value(data, option.key)
                    value = self._read_editor(option, editor, warnings)
                    if self._is_default_option_value(option, value) and not (
                        existing not in (None, "", {}, [])
                        and existing != option.default
                    ):
                        # Defaults (and empty text) are not persisted; the backend
                        # already falls back to them when the key is absent.
                        self._store(data, option.key, "")
                    else:
                        self._store(data, option.key, value)
                self.options_note.setText("  ".join(warnings))

                from ..core import artwork

                if data["custom_background"]:
                    artwork.set_art_source(data, "custom_background", artwork.SOURCE_USER)
                else:
                    artwork.art_source(data).pop("custom_background", None)
                data["remember_me"] = bool(self.remember.isChecked())
                config.data["language"] = (
                    self.language.currentData() or i18n.DEFAULT_LANGUAGE
                )
                config.data["check_updates"] = bool(self.check_updates.isChecked())
                for kind, path in self._pending_art.items():
                    artwork.record_thirdparty(data, path, kind)
                config.save()
        except OSError as exc:
            with config.locked():
                data.clear()
                data.update(backup[0])
                for key, old in zip(("language", "check_updates"), backup[1:], strict=True):
                    if old is None:
                        config.data.pop(key, None)
                    else:
                        config.data[key] = old
            QMessageBox.critical(self, "Settings", f"Could not save config: {exc}")
            return
        self._pending_art = {}
        i18n.set_language(config.data["language"])
        self.note.setText(f"Saved to {config.path}")
        self._loaded_state = self._form_state()
        self.saved.emit(self._profile)
        if warnings:
            # The page closes on ``saved``, so the inline note is never seen: say it here.
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "Settings saved",
                "Your settings were saved, but please check:\n\n- " + "\n- ".join(warnings),
                QMessageBox.StandardButton.Ok,
                self.window(),
            )
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()  # non-blocking


__all__ = [
    "SettingsPage",
    "discover_runners",
    "SYSTEM_WINE",
    "INT_RANGES",
    "DEFAULT_INT_RANGE",
]
