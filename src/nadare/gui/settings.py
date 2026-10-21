# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Settings page: runner picker, game folder, language, update check, background.

All values are persisted through :class:`~nadare.core.config.Config`, never by
hand. Runner enumeration prefers ``core.runners`` and degrades gracefully to a
scan of the Nadare runners directory when that module is unavailable.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
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

from ..core import paths
from ..core.backends import BackendError, Option, kinds, supports_remember_me
from ..core.backends import get as backend_get
from ..core.config import Config
from . import i18n
from .workers import CallTask

#: Label used for the "no runner" choice.
SYSTEM_WINE = "System Wine"

#: ``(min, max)`` for ``int`` options that need a tighter bound than the default.
INT_RANGES: dict[str, tuple[int, int]] = {"download_workers": (1, 16)}
DEFAULT_INT_RANGE = (0, 999999)


def _add_runner(entries: list[tuple[str, str]], seen: set[str], label: str, value: str) -> None:
    value = str(value or "")
    key = value or "__system__"
    if key in seen:
        return
    seen.add(key)
    entries.append((str(label), value))


def _runners_from_module(module: Any) -> Iterable[tuple[str, str]]:
    """Best-effort adapter over possible ``core.runners`` APIs."""
    candidates: Any = None
    for name in (
        "list_runners",
        "find_runners",
        "available_runners",
        "discover",
        "detected_runners",
    ):
        func = getattr(module, name, None)
        if callable(func):
            try:
                candidates = func()
            except Exception:  # noqa: BLE001 - optional module, never fatal
                candidates = None
            if candidates:
                break
    if not candidates:
        candidates = getattr(module, "RUNNERS", None)

    out: list[tuple[str, str]] = []
    if isinstance(candidates, dict):
        for key, info in candidates.items():
            if isinstance(info, dict):
                label = info.get("name") or info.get("label") or str(key)
                value = info.get("path") or info.get("tag") or str(key)
            else:
                label, value = str(key), str(info)
            out.append((str(label), str(value)))
    elif isinstance(candidates, (list, tuple, set)):
        for item in candidates:
            if isinstance(item, dict):
                label = item.get("name") or item.get("label") or item.get("path")
                value = item.get("path") or item.get("tag") or label
                if label:
                    out.append((str(label), str(value)))
            elif isinstance(item, str):
                out.append((item, item))
            else:
                label = getattr(item, "name", None)
                value = getattr(item, "path", None) or label
                if label:
                    out.append((str(label), str(value)))
    return out


def discover_runners() -> list[tuple[str, str]]:
    """Return ``(label, value)`` pairs for the runner picker.

    Value is a filesystem path for installed Proton builds, a tag/identifier for
    ones managed by a runner module, or ``""`` for system Wine.
    """
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()

    try:
        from ..core import runners as runners_module  # type: ignore
    except Exception:  # noqa: BLE001 - module may not exist yet
        runners_module = None
    if runners_module is not None:
        for label, value in _runners_from_module(runners_module):
            _add_runner(entries, seen, label, value)

    base = paths.runners_dir()
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        if not (child / "proton").exists():
            continue
        _add_runner(entries, seen, f"Proton · {child.name}", str(child))

    _add_runner(entries, seen, SYSTEM_WINE, "")
    return entries


def _env_to_text(value: Any) -> str:
    """Render an env mapping as sorted ``KEY=VALUE`` lines."""
    if not isinstance(value, dict):
        return ""
    return "\n".join(f"{key}={val}" for key, val in sorted(value.items()))


def _text_to_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a mapping (blank lines ignored)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key:
            out[key] = value.strip()
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._config: Config | None = None
        self._profile = ""
        # Every backend option key maps to its editor widget. "install_dir" and
        # "runner" live in the Game files card; the rest are built per backend.
        self._option_widgets: dict[str, QWidget] = {}
        self._option_free: dict[str, QLabel] = {}
        self._option_rows: list[tuple[bool, int]] = []
        self._options_form: QFormLayout | None = None
        self._fetch_button: QPushButton | None = None
        self._pending_art: dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 28)
        root.setSpacing(16)

        header = QHBoxLayout()
        self.back = QPushButton("←  Back")
        self.back.setObjectName("GhostButton")
        self.back.clicked.connect(self.cancelled.emit)
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
        profile_form.addRow("Profile", self.profile_name)
        profile_form.addRow("Title", self.title)
        content_layout.addWidget(profile_card)

        # ---- account --------------------------------------------------------
        account_card, account_form = self._section(
            "Account", "Whether this device remembers the STOVE login for this game."
        )
        self.remember = QCheckBox("Remember me on this device")
        self.remember_hint = QLabel(
            "Stores the login in your system keyring. Off by default; turning it off "
            "deletes the saved login immediately."
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
        self.install_dir_free = QLabel("")
        self.install_dir_free.setObjectName("Muted")
        files_form.addRow("", self.install_dir_free)
        files_form.addRow("Runner", self.runner)
        content_layout.addWidget(files_card)

        # ---- game options ---------------------------------------------------
        options_card, options_form = self._section(
            "Game options",
            "Backend-specific settings. Leave a field empty to use the backend default.",
        )
        self.show_advanced = QCheckBox("Show advanced options")
        self.show_advanced.toggled.connect(self._apply_advanced)
        options_form.addRow("", self.show_advanced)
        self.options_note = QLabel("")
        self.options_note.setObjectName("Muted")
        self.options_note.setWordWrap(True)
        options_form.addRow("", self.options_note)
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
            self.language.addItem(code)
        self.check_updates = QCheckBox("Check for launcher updates on start")
        launcher_form.addRow("Language", self.language)
        launcher_form.addRow("", self.check_updates)
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
        cancel = QPushButton("Cancel")
        cancel.setObjectName("GhostButton")
        cancel.clicked.connect(self.cancelled.emit)
        save = QPushButton("Save changes")
        save.setObjectName("Primary")
        save.setMinimumHeight(44)
        save.clicked.connect(self.save)
        footer.addWidget(cancel)
        footer.addWidget(save)
        root.addLayout(footer)

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

    def _reload_runners(self, selected: str) -> None:
        self.runner.clear()
        for label, value in discover_runners():
            self.runner.addItem(label, value)
        index = self.runner.findData(selected) if selected else -1
        if index < 0 and selected:
            self.runner.insertItem(0, f"Missing · {selected}", selected)
            index = 0
        if index < 0:
            index = self.runner.findData("")
        self.runner.setCurrentIndex(max(0, index))

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
        # install_dir / runner are always edited in the Game files card.
        self._option_widgets = {"install_dir": self.install_dir, "runner": self.runner}
        for option in self._backend_options(kind):
            if option.key in ("install_dir", "runner"):
                continue
            row_widget, editor = self._create_editor(option)
            self._option_widgets[option.key] = editor
            label = QLabel(option.label)
            if option.help:
                label.setToolTip(option.help)
                row_widget.setToolTip(option.help)
            form.addRow(label, row_widget)
            self._option_rows.append((option.advanced, form.rowCount() - 1))
        self._apply_advanced(self.show_advanced.isChecked())

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
        """Read ``key`` from the profile's top level or its ``extra`` mapping."""
        value = data.get(key)
        if value in (None, ""):
            extra = data.get("extra")
            if isinstance(extra, dict):
                value = extra.get(key)
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
            self._reload_runners(text)
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
            return editor.text().strip()
        if kind == "env":
            return _text_to_env(editor.toPlainText())
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
    def _store(data: dict[str, Any], key: str, value: Any) -> None:
        """Write ``key`` at the top level, or remove it when the value is empty."""
        if value is None or value == "" or value == {} or value == []:
            data.pop(key, None)
            extra = data.get("extra")
            if isinstance(extra, dict):
                extra.pop(key, None)
        else:
            data[key] = value

    # ---- load / save -------------------------------------------------------
    def load(self, config: Config, profile: str) -> None:
        """Populate the form from ``config`` for ``profile``."""
        self._config = config
        self._profile = profile
        self._pending_art = {}
        data = config.profile(profile)

        self.profile_name.setText(profile)
        self.heading.setText(f"Settings — {profile}")
        self.title.setText(str(data.get("title") or ""))
        kind = str(data.get("backend") or "")
        self._backend_kind = kind
        self.install_dir.setText(str(data.get("install_dir") or ""))
        self._update_free_hints()
        self._reload_runners(str(data.get("runner") or ""))
        self.runner.setEditText(str(data.get("runner") or ""))
        self.background.setText(str(data.get("custom_background") or ""))
        self._load_options(kind, data)
        self._update_free_hints()

        supported = supports_remember_me(str(data.get("backend") or ""))
        self.account_card.setVisible(supported)
        self.remember.setChecked(bool(data.get("remember_me", False)))

        language = str(config.data.get("language") or i18n.DEFAULT_LANGUAGE)
        lang_index = self.language.findText(language)
        self.language.setCurrentIndex(lang_index if lang_index >= 0 else 0)
        self.check_updates.setChecked(bool(config.data.get("check_updates", True)))
        self.note.setText(f"Saved in {config.path}")

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
        try:
            with config.locked():
                data["title"] = self.title.text().strip()
                data["backend"] = backend
                self._store(data, "install_dir", self.install_dir.text().strip())
                self._store(data, "runner", str(runner_value or ""))
                data["custom_background"] = self.background.text().strip()

                warnings: list[str] = []
                for option in self._backend_options(data["backend"]):
                    if option.key in ("install_dir", "runner"):
                        continue
                    editor = self._option_widgets.get(option.key)
                    if editor is None:
                        continue
                    self._store(data, option.key, self._read_editor(option, editor, warnings))
                self.options_note.setText("  ".join(warnings))

                from ..core import artwork

                if data["custom_background"]:
                    artwork.set_art_source(data, "custom_background", artwork.SOURCE_USER)
                else:
                    artwork.art_source(data).pop("custom_background", None)
                data["remember_me"] = bool(self.remember.isChecked())
                config.data["language"] = self.language.currentText().strip() or "en"
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
        self.saved.emit(self._profile)


__all__ = [
    "SettingsPage",
    "discover_runners",
    "SYSTEM_WINE",
    "INT_RANGES",
    "DEFAULT_INT_RANGE",
]
