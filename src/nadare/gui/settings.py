# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Settings page: runner picker, game folder, language, update check, background.

All values are persisted through :class:`~nadare.core.config.Config`, never by
hand. Runner enumeration prefers ``core.runners`` and degrades gracefully to a
scan of the Nadare runners directory when that module is unavailable.
"""

from __future__ import annotations

import copy as copy  # noqa: E402, F401
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal

# Names that tests and callers reach through this module; the code using them moved to
# settings_parts/ (#1248). Re-exported so ``settings.X`` is the same object.
from PyQt6.QtGui import QDesktopServices as QDesktopServices  # noqa: E402, F401
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import app_uninstall as app_uninstall  # noqa: E402, F401
from ..core import community as community  # noqa: E402, F401
from ..core import gamefolders, importmove
from ..core import libraries as core_libraries
from ..core import relink as relink_core
from ..core.backends import Option
from ..core.config import Config
from . import i18n
from .libraries_panel import LibrariesPanel
from .library import FlowLayout
from .library_picker import LibraryPicker, resolve_other_folder
from .settings_parts.extras import (
    SYSTEM_WINE,
    ExtrasMixin,
    _is_runner_folder,
    _runner_label,
    discover_runners,
)
from .settings_parts.move import (
    MoveMixin,
    _MoveSignals,
)
from .settings_parts.options import (
    _ENV_TOKEN,
    DEFAULT_INT_RANGE,
    INT_RANGES,
    LEGACY_PROFILE_KEYS,
    PLAYER_ADVANCED,
    OptionsMixin,
    _clear_layout,
    _env_to_text,
    args_env_warning,
)
from .settings_parts.persistence import (
    APP_SUBTITLE,
    PersistenceMixin,
)
from .settings_parts.prefix_tools import (
    PrefixToolsMixin,
)
from .settings_parts.shared import (
    _ENV_NAME,
    GAME_SUBTITLE,
    _fill_monitor_combo,
    _text_to_env,
)

#: Native language names for the picker; an unknown code shows as-is.
LANGUAGE_NAMES = {"en": "English"}

#: Help under the Settings "Remember me" tick.
REMEMBER_HINT = (
    "Keeps you logged in on this device, so you do not need to sign in each time. "
    "Off by default; turning it off deletes the saved login when you press Save."
)


#: Plain names of the folder settings, for the "points outside the game folder" note.
_FOLDER_KEY_NAMES = {"install_dir": "Game folder", "prefix": "Wine prefix"}


def libraries_display(path: Path) -> str:
    return core_libraries.display_path(path)


class SettingsPage(
    ExtrasMixin,
    MoveMixin,
    OptionsMixin,
    PersistenceMixin,
    PrefixToolsMixin,
    QWidget,
):
    """Editable settings for one profile plus the shared language/update options."""

    saved = pyqtSignal(str)  # profile name
    #: A sentence to show after the page closes (the side status): profile, text.
    notice = pyqtSignal(str, str)
    #: A changed WuWa quality whose files the player wants now (#1335).
    download_requested = pyqtSignal(str)  # profile name
    cancelled = pyqtSignal()
    remove_requested = pyqtSignal(str)  # profile name
    remake_prefix_requested = pyqtSignal(str)  # profile name
    report_requested = pyqtSignal(str)
    logs_requested = pyqtSignal(str)
    system_check_requested = pyqtSignal()
    games_added = pyqtSignal(list)  # profile names brought back from a library
    games_repaired = pyqtSignal(list)  # profile names pointed at a folder found elsewhere

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
        self._backup_label: QLabel | None = None
        self._backup_button: QPushButton | None = None
        # Rebuilt with the options form (the old one is deleted with its page).
        self.relink_button = self._make_relink_button()
        self.move_button = self._make_move_button()
        self.change_location_button = self._make_change_location_button()
        self._moving = False
        self._game_active = False  # installed or running: no Change location
        self._move_running = False
        self._game_idle = False  # known idle only after a successful status probe
        self._prefix_lock_generation = 0
        # Relinks waiting for their "is it running?" answer, by ticket: two games can each have
        # one in flight, and an answer must finish its own plan only (final round).
        self._pending_relink: dict[int, tuple[Config, str, relink_core.Relink, str]] = {}
        self._relink_ticket = 0
        self._probed_prefix = ""  # where Nadare says the prefix is (the page may have none set)
        self._fetch_button: QPushButton | None = None
        self._pending_art: dict[str, str] = {}
        self._loaded_state: dict[str, Any] | None = None
        self._app_only = False

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
        self.subtitle = QLabel(GAME_SUBTITLE)
        self.subtitle.setObjectName("Subtitle")
        intro.addWidget(self.subtitle)
        root.addLayout(intro)

        # One tab per area. Each tab scrolls on its own so the cards never clip on short displays.
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        general_layout = self._tab_page("General")
        game_layout = self._tab_page("Game")
        launcher_tab_layout = self._tab_page("Nadare (all games)")
        self._app_tab = self.tabs.count() - 1

        # ---- profile --------------------------------------------------------
        profile_card, profile_form = self._section(
            "Game", "Basic identity. The game type is fixed when the game is added."
        )
        self.profile_name = QLineEdit()
        self.profile_name.setReadOnly(True)
        self.title = QLineEdit()
        self.title.setPlaceholderText("name shown in the library")
        # The backend is chosen once, in Add game. Switching it afterwards would
        # point the game's existing prefix and files at the wrong driver.
        self._backend_kind = ""
        profile_form.addRow("Game ID", self.profile_name)
        profile_form.addRow("Game name", self.title)
        general_layout.addWidget(profile_card)

        # ---- account --------------------------------------------------------
        account_card, account_form = self._section(
            "Account", "Whether this device remembers the STOVE login for this game."
        )
        self.remember = QCheckBox("Remember me on this device")
        self.remember_hint = QLabel(REMEMBER_HINT)
        self.remember_hint.setObjectName("Subtitle")
        self.remember_hint.setWordWrap(True)
        account_form.addRow("", self.remember)
        account_form.addRow("", self.remember_hint)
        self.account_card = account_card
        general_layout.addWidget(account_card)

        # ---- game files -----------------------------------------------------
        files_card, files_form = self._section(
            "Game files", "Where the game lives and which compatibility layer runs it."
        )
        self.install_dir = QLineEdit()
        self.install_dir.editingFinished.connect(self._update_free_hints)
        self.runner = QComboBox()
        self.runner.setEditable(True)
        self.runner.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        install_row = self._with_browse(self.install_dir, "dir")
        self._install_dir_browse = install_row.findChildren(QPushButton)[0]
        files_form.addRow("Game folder", install_row)
        self._files_form = files_form
        self._install_dir_row = files_form.rowCount() - 1
        self.install_dir_free = QLabel("")
        self.install_dir_free.setObjectName("Muted")
        files_form.addRow("", self.install_dir_free)
        # Where the game is, the Steam way (#1229): its library, or "Outside your libraries".
        self.place_label = QLabel("")
        self.place_label.setObjectName("Muted")
        self.place_label.setWordWrap(True)
        self.place_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.place_label.setVisible(False)
        self.reset_stray_button = QPushButton("Use the game folder's own paths")
        self.reset_stray_button.setObjectName("GhostButton")
        self.reset_stray_button.setToolTip(
            "Point the settings back at the game's own folder. Nothing on disk is moved or deleted."
        )
        self.reset_stray_button.clicked.connect(self._reset_stray)
        self.reset_stray_button.setVisible(False)
        place_box = QWidget()
        place_layout = QVBoxLayout(place_box)
        place_layout.setContentsMargins(0, 0, 0, 0)
        place_layout.setSpacing(4)
        place_layout.addWidget(self.place_label)
        reset_row = QHBoxLayout()
        self.import_move_button = QPushButton("Move into a library…")
        self.import_move_button.setObjectName("GhostButton")
        self.import_move_button.setToolTip(
            "Move this game's folder and Wine prefix into one of your libraries, where Nadare "
            "manages it like a game it installed."
        )
        self.import_move_button.clicked.connect(self._move_import)
        self.import_move_button.setVisible(False)
        reset_row.addWidget(self.import_move_button)
        reset_row.addWidget(self.reset_stray_button)
        reset_row.addStretch(1)
        place_layout.addLayout(reset_row)
        files_form.addRow("", place_box)
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
        open_row = QWidget()
        open_layout = QHBoxLayout(open_row)
        open_layout.setContentsMargins(0, 0, 0, 0)
        self.open_game_button = QPushButton("Open game folder")
        self.open_game_button.setObjectName("GhostButton")
        self.open_game_button.clicked.connect(self._open_game_folder)
        self.open_prefix_button = QPushButton("Open Wine prefix")
        self.open_prefix_button.setObjectName("GhostButton")
        self.open_prefix_button.clicked.connect(self._open_prefix_folder)
        open_layout.addWidget(self.open_game_button)
        open_layout.addWidget(self.open_prefix_button)
        open_layout.addStretch(1)
        files_form.addRow("Open", open_row)
        game_layout.addWidget(files_card)

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
        game_layout.addWidget(options_card)

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
        general_layout.addWidget(appearance_card)

        # ---- launcher -------------------------------------------------------
        launcher_card, launcher_form = self._section(
            "Nadare — all games",
            "These settings belong to Nadare itself and apply to every game in Nadare, "
            "not only this one.",
        )
        self.language = QComboBox()
        for code in i18n.available():
            self.language.addItem(LANGUAGE_NAMES.get(code, code), code)
        self.language_label = QLabel("Language")
        self.check_updates = QCheckBox("Check for launcher and game updates")
        self.check_notices = QCheckBox("Show safety and service notices")
        self.check_notices.setToolTip(
            "Nadare reads a small public notices file at start-up, for example a safety "
            "warning about a game launcher. It sends nothing about you. Leave this on "
            "unless you are offline on purpose."
        )
        self.notify_when_done = QCheckBox("Tell me when an install or update finishes")
        self.notify_when_done.setToolTip(
            "Shows a desktop notification when an install, update or repair ends while Nadare "
            "is in the background. It needs a desktop that shows notifications."
        )
        self.hide_uninstalled = QCheckBox("Hide games that are not installed in the sidebar")
        launcher_form.addRow(self.language_label, self.language)
        launcher_form.addRow("", self.check_updates)
        launcher_form.addRow("", self.check_notices)
        launcher_form.addRow("", self.notify_when_done)
        launcher_form.addRow("", self.hide_uninstalled)
        self.default_monitor = QComboBox()
        self.default_monitor.setToolTip(
            "The monitor every game opens on, unless a game has its own choice. "
            "Works only with native Wayland. Steam games are not affected."
        )
        _fill_monitor_combo(self.default_monitor)
        launcher_form.addRow("Monitor for all games", self.default_monitor)
        self.default_monitor_hint = QLabel(
            "Applies to all games, not to one installation. A game's own Monitor setting "
            "wins; Steam games are not affected. Works only with native Wayland."
        )
        self.default_monitor_hint.setObjectName("Muted")
        self.default_monitor_hint.setWordWrap(True)
        launcher_form.addRow("", self.default_monitor_hint)
        # In the body, not the footer: at the minimum window width the footer already clips.
        self.system_check_button = QPushButton("System check")
        self.system_check_button.setObjectName("GhostButton")
        self.system_check_button.setToolTip(
            "Check graphics, Vulkan and runners. Nothing is changed or uploaded."
        )
        self.system_check_button.clicked.connect(self.system_check_requested)
        self.system_check_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        launcher_form.addRow("", self.system_check_button)
        self.discord_button = QPushButton("Join our Discord")
        self.discord_button.setObjectName("GhostButton")
        self.discord_button.setToolTip("Open the Nadare community on Discord in your browser.")
        self.discord_button.clicked.connect(self._open_discord)
        self.discord_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        launcher_form.addRow("", self.discord_button)
        self.open_config_button = QPushButton("Open config file")
        self.open_config_button.setObjectName("GhostButton")
        self.open_config_button.setToolTip("Open the file where Nadare keeps all your settings.")
        self.open_config_button.clicked.connect(self._open_config_file)
        self.open_config_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        launcher_form.addRow("", self.open_config_button)
        self.uninstall_nadare_button = QPushButton("Uninstall Nadare…")
        self.uninstall_nadare_button.setObjectName("GhostButton")
        self.uninstall_nadare_button.setToolTip(
            "Remove the application while keeping games and settings."
        )
        self.uninstall_nadare_button.clicked.connect(self._uninstall_nadare)
        self.uninstall_nadare_button.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        launcher_form.addRow("", self.uninstall_nadare_button)
        # With a single shipped locale the picker is meaningless: hide the row
        # until a second catalogue makes it actionable.
        multiple_languages = len(i18n.available()) > 1
        self.language_label.setVisible(multiple_languages)
        self.language.setVisible(multiple_languages)
        launcher_tab_layout.addWidget(launcher_card)

        # ---- libraries (Steam-like; Add/Remove act at once, not on Save) ----------------
        libraries_card, libraries_form = self._section(
            "Libraries",
            "Folders Nadare installs games into. A library may be on another drive and may "
            "hold other files: games go in a Nadare folder inside it. Removing a library "
            "only forgets it; its games and files stay.",
        )
        self.libraries_panel = LibrariesPanel(self)
        self.libraries_panel.games_added.connect(self.games_added)
        self.libraries_panel.games_repaired.connect(self.games_repaired)
        libraries_form.addRow(self.libraries_panel)
        launcher_tab_layout.addWidget(libraries_card)
        for layout in (general_layout, game_layout, launcher_tab_layout):
            layout.addStretch(1)
        root.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        # The three secondary buttons wrap onto a second line when the window is narrow; Save
        # stays whole on the right.
        actions_box = QWidget()
        actions = FlowLayout(actions_box, spacing=8)
        self.remove_button = QPushButton("Uninstall or remove…")
        self.remove_button.setObjectName("GhostButton")
        self.remove_button.setToolTip(
            "Remove this game from Nadare, or uninstall it by also ticking its game files. "
            "Game files are kept by default."
        )
        self.remove_button.clicked.connect(self._request_remove)
        actions.addWidget(self.remove_button)
        self.report_button = QPushButton("Report a problem")
        self.report_button.setObjectName("GhostButton")
        self.report_button.clicked.connect(lambda: self.report_requested.emit(self._profile))
        actions.addWidget(self.report_button)
        self.logs_button = QPushButton("Open logs")
        self.logs_button.setObjectName("GhostButton")
        self.logs_button.clicked.connect(lambda: self.logs_requested.emit(self._profile))
        actions.addWidget(self.logs_button)
        # The note ("Saved in <config path>") gets its own line above the buttons: in the row it
        # squeezed every button below its text at the smallest window (b23-rc4 click-through).
        self.note = QLabel("")
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        self.note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(actions_box, 1)
        save = QPushButton("Save changes")
        save.setObjectName("Primary")
        save.setMinimumHeight(44)
        save.clicked.connect(self.save)
        self.save_button = save
        footer.addWidget(save, 0, Qt.AlignmentFlag.AlignBottom)
        root.addWidget(self.note)
        root.addLayout(footer)

    def _tab_page(self, title: str) -> QVBoxLayout:
        """Add a scrolling tab called ``title`` and return the layout its cards go into."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("SettingsContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 12, 2, 2)
        layout.setSpacing(16)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)
        return layout

    def confirm_leave(self) -> bool:
        """True when it is fine to leave: nothing unsaved, or the user agreed to discard it."""
        if not self._has_unsaved_changes():
            return True
        answer = QMessageBox.question(
            self,
            "Discard changes",
            "Discard your changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._loaded_state = self._form_state()  # discarded: the form no longer counts as edited
        return True

    def _request_back(self) -> None:
        """Leave Settings, asking first when the form has unsaved edits."""
        if self.confirm_leave():
            self.cancelled.emit()

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
        # A long subtitle must wrap, or it sets the whole tab's minimum width (the Libraries
        # card's pushed its Remove button off-screen at the minimum window width).
        hint.setWordWrap(True)
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

    def _choose_library(self, config: Config, title: str, start: str) -> str | None:
        """Pick a library from the list, or another folder through the folder dialog.

        Tests replace this method; keep the mapping and checks in the callers.
        """
        picker = LibraryPicker(config, title, self, include_default=False)
        try:
            accepted = picker.exec() == QDialog.DialogCode.Accepted
            chosen = picker.chosen() if accepted else None
        finally:
            picker.deleteLater()
        if not accepted:
            return None
        if chosen is not None:
            return str(chosen)
        # "Another folder…" (and no other accepted row) opens the ordinary folder dialog.
        picked = QFileDialog.getExistingDirectory(self, title, start)
        return picked or None

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
            self._prefix_lock_note = QLabel(self._LOCKED_NOTE)
            self._prefix_lock_note.setObjectName("Muted")
            self._prefix_lock_note.setWordWrap(True)
            self._prefix_lock_note.setVisible(False)
            layout.addWidget(self._prefix_lock_note)
            self._backup_label = QLabel("")
            self._backup_label.setObjectName("Muted")
            self._backup_label.setWordWrap(True)
            self._backup_label.setVisible(False)
            self._backup_button = QPushButton("Delete old backup")
            self._backup_button.setObjectName("GhostButton")
            self._backup_button.setVisible(False)
            self._backup_button.clicked.connect(self._delete_old_backup)
            backup_row = QHBoxLayout()
            backup_row.addWidget(self._backup_label, 1)
            backup_row.addWidget(self._backup_button)
            layout.addLayout(backup_row)
            self.relink_button = self._make_relink_button()
            self.move_button = self._make_move_button()
            self.change_location_button = self._make_change_location_button()
            row = QHBoxLayout()
            row.addWidget(self.move_button)
            row.addWidget(self.relink_button)
            row.addWidget(self.change_location_button)
            row.addStretch(1)
            layout.addLayout(row)
        return wrapper

    def _make_change_location_button(self) -> QPushButton:
        button = QPushButton("Change location…")
        button.setAutoDefault(False)
        button.setVisible(False)
        button.clicked.connect(self._change_location)
        return button

    #: Shown under the locked prefix field (the note is reworded while a check is pending/failed).
    _LOCKED_NOTE = (
        "The game is installed, so this location is locked. Editing it here would make "
        "Nadare would no longer find the game. Use the Move button to relocate it, or "
        "'I moved it myself' if you already did."
    )

    def _apply_advanced(self, show: bool) -> None:
        if self._options_form is None:
            return
        for advanced, row in self._option_rows:
            self._options_form.setRowVisible(row, bool(show) or not advanced)

    def _in_folder_model(self) -> bool:
        if self._config is None or not self._profile:
            return False
        data = self._config.profiles.get(self._profile)
        return isinstance(data, dict) and gamefolders.in_model(data)

    def _show_placement(self) -> None:
        """Say where the game is (#1229): its library, or outside them, and any stray path."""
        place = (
            gamefolders.placement(self._config, self._profile)
            if self._config is not None and self._profile
            else None
        )
        self.import_move_button.setVisible(False)
        if place is None or place.folder is None:
            self.place_label.setVisible(False)
            self.reset_stray_button.setVisible(False)
            return
        folder = str(place.folder)
        if place.library is not None and place.made_by_nadare:
            text = f"Game folder: {folder}\nIn your library {libraries_display(place.library)}."
        elif place.library is not None:
            text = (
                f"Game folder: {folder}\nIn your library {libraries_display(place.library)}. "
                "You added it as an existing install, so Nadare never deletes this folder."
            )
        elif place.made_by_nadare:
            text = (
                f"Game folder: {folder}\nOutside your libraries (its library is no longer "
                "listed). Add the library again, or use Move to put the game into one."
            )
        else:
            # No "Move into a library" yet: for an import, Move only moves the Wine prefix.
            text = (
                f"Game folder: {folder}\nOutside your libraries: Nadare uses the game where it "
                "is and never deletes this folder."
            )
        resettable = [key for key, value in place.stray.items() if value is None or value]
        if place.stray:
            names = ", ".join(_FOLDER_KEY_NAMES.get(key, key) for key in place.stray)
            text += f"\nThese settings point outside the game folder: {names}."
        self.place_label.setText(text)
        self.place_label.setVisible(True)
        self.reset_stray_button.setVisible(bool(resettable))
        self.import_move_button.setVisible(not place.made_by_nadare)

    def _move_import(self) -> None:
        """Move an import into a library (#1229): pick, check, ask, then run the move."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        if not self._game_idle or self._moving:
            return
        roots = core_libraries.library_roots(config)
        start = str(roots[1] if len(roots) > 1 else Path.home())
        library = self._choose_library(config, "Move into which library?", start)
        if not library:
            return
        library = str(resolve_other_folder(library))
        try:
            move = importmove.plan(config, name, library)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Move into a library", str(exc))
            return
        sources = "\n".join(f"  {source}" for _, source, _ in move.trees)
        answer = QMessageBox.question(
            self,
            "Move into a library?",
            f"Move\n{sources}\ninto {move.home}?\n\nOn the same disk this is instant. On "
            "another disk Nadare copies, checks the copy, and keeps your original.\n\n"
            "Afterwards Nadare manages the game like one it installed: Uninstall can delete it.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_move(config, name, move.trees[0][1], move.home, import_move=move)

    def _reset_stray(self) -> None:
        """Point stray folder settings back at the game folder and save (nothing on disk moves)."""
        config, name = self._config, self._profile
        if config is None or not name:
            return
        try:
            with config.locked():
                changed = gamefolders.reset_stray(config, name)
                if changed:
                    config.save()
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not save the settings: {exc}")
            return
        if changed:
            self.load(config, name)

__all__ = [
    "SettingsPage",
    "discover_runners",
    "SYSTEM_WINE",
    "INT_RANGES",
    "DEFAULT_INT_RANGE",
    "SYSTEM_WINE",
    "_runner_label",
    "discover_runners",
    "_is_runner_folder",
    "_MoveSignals",
    "INT_RANGES",
    "DEFAULT_INT_RANGE",
    "PLAYER_ADVANCED",
    "LEGACY_PROFILE_KEYS",
    "_env_to_text",
    "_ENV_TOKEN",
    "args_env_warning",
    "_clear_layout",
    "APP_SUBTITLE",
    "GAME_SUBTITLE",
    "_ENV_NAME",
    "_fill_monitor_combo",
    "_text_to_env",
]
