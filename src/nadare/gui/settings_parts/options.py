# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The per-game option editors: building them, reading them and checking their values.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import runners as runners_core
from ...core.backends import Option, config_only
from ...core.backends import get as backend_get
from ...core.config import as_bool, profile_value
from .shared import (
    _fill_monitor_combo,
    _text_to_env,
)

#: ``(min, max)`` for ``int`` options that need a tighter bound than the default.
INT_RANGES: dict[str, tuple[int, int]] = {"download_workers": (1, 16)}

DEFAULT_INT_RANGE = (0, 999999)

PLAYER_ADVANCED = frozenset({"renderer", "display", "launch_args"})

#: Old beta keys mapped to the canonical field that now owns their value.
LEGACY_PROFILE_KEYS = {"runner": "proton_runner", "prefix": "wineprefix"}


def _env_to_text(value: Any) -> str:
    """Render an env mapping as sorted ``KEY=VALUE`` lines."""
    if not isinstance(value, dict):
        return ""
    return "\n".join(f"{key}={val}" for key, val in sorted(value.items()))

_ENV_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def args_env_warning(text: str) -> str | None:
    """Explain why environment text in Launch arguments will not work, else ``None``.

    The field is argv appended after the executable, so Steam's
    ``VAR=value %command%`` reaches the game as plain arguments (#779). A
    ``KEY=value`` token is only flagged when the key is upper case with no leading
    dash, so ``--width=1920`` and ``-logFile=a`` pass.
    """
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if "%command%" in tokens or any(
        _ENV_TOKEN.match(token) and token.split("=", 1)[0].isupper() for token in tokens
    ):
        return (
            "Launch arguments: this looks like environment variables (KEY=value or "
            "%command%), which the game receives as plain arguments and ignores. Put "
            "variables in the Environment field instead."
        )
    return None


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


class OptionsMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    # ---- backend options ---------------------------------------------------
    @staticmethod
    def _backend_options(kind: str) -> tuple[Option, ...]:
        """The declared options for ``kind`` (empty for unknown backends)."""
        try:
            options = backend_get(kind).options
        except Exception:  # noqa: BLE001 - an unknown backend simply has none
            return ()
        # Config-only options keep working from the profile but get no editor, so a
        # Save never touches them (#778).
        return tuple(option for option in options if not config_only(kind, option.key))

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
        self._backup_label = None
        self._backup_button = None
        self.relink_button = self._make_relink_button()
        self.move_button = self._make_move_button()
        self.change_location_button = self._make_change_location_button()
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
            self._option_rows.append(
                (option.advanced or option.key in PLAYER_ADVANCED, form.rowCount() - 1)
            )
        self.open_game_button.setVisible(has_install_dir)
        self.open_prefix_button.setVisible(any(option.key == "prefix" for option in options))
        self._apply_advanced(self.show_advanced.isChecked())

    def _restore_option_defaults(self) -> None:
        """Put every backend option editor back to its default (without saving)."""
        for option in self._backend_options(self._backend_kind):
            if option.key in ("install_dir", "runner"):
                continue
            editor = self._option_widgets.get(option.key)
            if editor is None:
                continue
            if option.key == "prefix" and getattr(editor, "isReadOnly", lambda: False)():
                continue  # locked for an installed game: clearing it would repoint the game
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
            elif kind == "monitor":
                # Back to Auto (the launcher's default monitor), like every other option (b221).
                index = editor.findData("")
                editor.setCurrentIndex(index if index >= 0 else 0)
            elif kind in ("text", "args", "path_dir", "path_file"):
                editor.setText("")
            elif isinstance(editor, QLineEdit):
                editor.setText("")
        self._update_free_hints()

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
        if kind == "monitor":
            editor = QComboBox()
            _fill_monitor_combo(editor)
            return editor, editor
        if kind == "runner":
            return self.runner, self.runner
        editor = QLineEdit()
        return editor, editor

    @staticmethod
    def _option_value(data: dict[str, Any], key: str) -> Any:
        """Read ``key`` from the profile's top level or its ``extra`` mapping.

        Older betas wrote ``proton_runner``/``wineprefix``; show that value when
        the canonical field is empty so Settings does not look blank.
        """
        value = profile_value(data, key)
        if value in (None, ""):
            old = LEGACY_PROFILE_KEYS.get(key)
            if old:
                value = profile_value(data, old)
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
            editor.setChecked(
                bool(option.default)
                if value is None
                else bool(as_bool(value, bool(value)))
            )
        elif kind == "choice":
            index = editor.findData(value)
            if index < 0 and value not in (None, ""):
                index = editor.findText(str(value))
            if index < 0 and value not in (None, ""):
                # A newer build or a hand edit saved a value this one does not list:
                # keep it visible and save it back instead of silently choosing Auto.
                editor.addItem(f"{value} (not recognised)", value)
                index = editor.count() - 1
            if index < 0 and option.default is not None:
                # An unset key means the option's default, not the first entry: WuWa's quality
                # lists SD first, so any Settings save on a pre-#990 profile wrote "sd" and the
                # game crashed on files that were never downloaded (2026-11-14).
                index = editor.findData(option.default)
            editor.setCurrentIndex(index if index >= 0 else 0)
        elif kind == "monitor":
            _fill_monitor_combo(editor, value)
            index = editor.findData("" if value in (None, "auto") else str(value))
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

    def _refuse_broken_values(self, backend: str, runner_text: str, index: int) -> bool:
        """Say what would stop the game from starting, before anything is written.

        These used to be saved and then reported in a "saved, but please check" box, leaving
        the broken value in the config. Returns True (and focuses the field) when refusing.
        """
        problems: list[tuple[str, QWidget]] = []
        # Only text the user typed is checked: a saved runner that has since gone missing shows
        # as its bare value next to a "Missing · ..." item, and refusing it would block every
        # other change on this page.
        kept = index >= 0 and runner_text in (
            self.runner.itemText(index),
            str(self.runner.itemData(index) or ""),
        )
        if not kept and runner_text:
            try:
                runners_core.resolve_runner(runner_text)
            except runners_core.RunnerError:
                problems.append(
                    (
                        "Runner: that runner is not installed. Pick one from the list, or "
                        "leave the box empty to use the default.",
                        self.runner,
                    )
                )
        for option in self._backend_options(backend):
            editor = self._option_widgets.get(option.key)
            if option.key == "steam_app_id" and isinstance(editor, QLineEdit):
                # Steam app ids are numbers; anything else only fails later, at launch (#331).
                value = editor.text().strip()
                if value and not value.isdigit():
                    problems.append(
                        (f"{option.label}: must be a number, e.g. 4813850.", editor)
                    )
                continue
            if option.kind != "args" or not isinstance(editor, QLineEdit):
                continue
            value = editor.text().strip()
            try:
                shlex.split(value)
            except ValueError:
                problems.append(
                    (
                        f"{option.label}: has an opening quote with no closing quote, so "
                        "the game would fail to start.",
                        editor,
                    )
                )
        if not problems:
            return False
        QMessageBox.warning(
            self,
            "Settings not saved",
            "Your settings were not saved. Fix this first:\n\n- "
            + "\n- ".join(text for text, _widget in problems),
        )
        problems[0][1].setFocus()
        return True

    def _read_editor(self, option: Option, editor: QWidget, warnings: list[str]) -> Any:
        kind = option.kind
        if kind in ("text", "args"):
            value = editor.text().strip()
            if kind == "args" and value:
                # Unbalanced quotes were refused before saving (_refuse_broken_values).
                message = args_env_warning(value)
                if message:
                    warnings.append(message)
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
        if kind in ("choice", "monitor"):
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
        if option.kind in ("text", "args", "path_dir", "path_file", "env", "monitor"):
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
