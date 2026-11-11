# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Settings page: runner picker, game folder, language, update check, background.

All values are persisted through :class:`~nadare.core.config.Config`, never by
hand. Runner enumeration prefers ``core.runners`` and degrades gracefully to a
scan of the Nadare runners directory when that module is unavailable.
"""

from __future__ import annotations

import copy
import re
import shlex
from pathlib import Path
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QFile, QObject, Qt, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
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
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import app_uninstall, community, monitors
from ..core import libraries as core_libraries
from ..core import process as process_core
from ..core import relink as relink_core
from ..core import relocate as relocate_core
from ..core import runners as runners_core
from ..core.backends import BackendError, Option, config_only, kinds, supports_remember_me
from ..core.backends import create as create_backend
from ..core.backends import get as backend_get
from ..core.config import Config
from . import i18n
from .libraries_panel import LibrariesPanel
from .library import FlowLayout, count_text, profile_title
from .workers import CallTask

#: Label used for the "no runner" choice.
SYSTEM_WINE = "System Wine"

#: ``(min, max)`` for ``int`` options that need a tighter bound than the default.
INT_RANGES: dict[str, tuple[int, int]] = {"download_workers": (1, 16)}
DEFAULT_INT_RANGE = (0, 999999)
#: Options every game page lists but only people who know what they do should touch:
#: shown under "Show advanced options", whatever the backend says.
GAME_SUBTITLE = "How this game is installed, launched and displayed."
APP_SUBTITLE = "Settings that belong to Nadare itself and apply to every game."
PLAYER_ADVANCED = frozenset({"renderer", "display", "launch_args"})

#: Old beta keys mapped to the canonical field that now owns their value.
LEGACY_PROFILE_KEYS = {"runner": "proton_runner", "prefix": "wineprefix"}

#: Native language names for the picker; an unknown code shows as-is.
LANGUAGE_NAMES = {"en": "English"}

#: Help under the Settings "Remember me" tick.
REMEMBER_HINT = (
    "Keeps you logged in on this device, so you do not need to sign in each time. "
    "Off by default; turning it off deletes the saved login when you press Save."
)


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


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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


def _fill_monitor_combo(combo: QComboBox, saved: Any = None) -> None:
    """Fill ``combo`` with Auto and the connected monitors.

    A saved name that is not connected here (a config copied from another PC) stays
    in the list, marked, so saving does not silently drop the choice.
    """
    name = str(saved or "").strip()
    combo.clear()
    combo.addItem("Auto", "")
    listed = set()
    for monitor in monitors.connected():
        combo.addItem(monitor.label, monitor.name)
        listed.add(monitor.name)
    if name and name != "auto" and name not in listed:
        combo.addItem(f"{name} (not connected)", name)


def _is_runner_folder(value: str) -> bool:
    """Whether ``value`` is a Proton or Wine tree Nadare can start (a custom runner)."""
    return runners_core.looks_like_runner(value)


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
        if sep and _ENV_NAME.fullmatch(key):
            out[key] = value.strip()
        elif sep and key and warnings is not None:
            warnings.append(
                f"Environment: '{key}' is not a valid variable name (letters, digits and "
                "underscores, not starting with a digit), so the line was not saved."
            )
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

class _MoveSignals(QObject):
    progress = pyqtSignal(object, object)  # bytes done, bytes total


class SettingsPage(QWidget):
    """Editable settings for one profile plus the shared language/update options."""

    saved = pyqtSignal(str)  # profile name
    #: A sentence to show after the page closes (the side status): profile, text.
    notice = pyqtSignal(str, str)
    cancelled = pyqtSignal()
    remove_requested = pyqtSignal(str)  # profile name
    remake_prefix_requested = pyqtSignal(str)  # profile name
    report_requested = pyqtSignal(str)
    logs_requested = pyqtSignal(str)
    system_check_requested = pyqtSignal()
    games_added = pyqtSignal(list)  # profile names brought back from a library

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
        self._moving = False
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

    def _uninstall_nadare(self) -> None:
        plan = app_uninstall.preview()
        if plan is None:
            QMessageBox.information(
                self, "Uninstall Nadare",
                "Remove Nadare using the tool that installed it: your software centre for "
                "Flatpak or packages, or Gear Lever / AppImageLauncher for a managed AppImage. "
                "For a source install, remove its checkout and virtual environment. "
                "A portable AppImage can be moved to Trash in your file manager. "
                "Keep application data to preserve games and settings.",
            )
            return
        answer = QMessageBox.question(
            self, "Uninstall Nadare",
            "For a portable AppImage, move this file to Trash?\n\n"
            f"{plan.path}\n\n"
            "Your games, saves, accounts, runners and settings will be kept. "
            "Other AppImage versions and shortcuts will be kept too.\n\n"
            "If you installed Nadare with Gear Lever or AppImageLauncher, cancel and "
            "remove it there instead, so its menu entry is also removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            app_uninstall.trash(plan, QFile.moveToTrash)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not uninstall Nadare", str(exc))
            return
        QMessageBox.information(
            self, "AppImage moved to Trash",
            "The AppImage is in Trash. Close Nadare when you have finished any running "
            "games or downloads. You can restore the AppImage from Trash.",
        )

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

    def _open_path(self, path: str, what: str) -> None:
        """Open ``path`` in the desktop's file manager/editor, or say why not."""
        text = path.strip()
        if not text:
            self.note.setText(f"No {what} is set yet.")
            return
        target = Path(text).expanduser()
        if not target.exists():
            self.note.setText(f"The {what} does not exist yet: {target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_game_folder(self) -> None:
        self._open_path(self.install_dir.text(), "game folder")

    def _open_prefix_folder(self) -> None:
        editor = self._option_widgets.get("prefix")
        text = editor.text() if isinstance(editor, QLineEdit) else ""
        self._open_path(text or self._probed_prefix, "Wine prefix")

    def _open_discord(self) -> None:
        """Look up the invite off the GUI thread (it may ask the index), then open it."""
        self.discord_button.setEnabled(False)
        task = CallTask(community.discord_url)
        task.signals.done.connect(self._discord_ready)
        task.signals.failed.connect(lambda _message: self._discord_ready(None))
        QThreadPool.globalInstance().start(task)

    def _discord_ready(self, url: object) -> None:
        self.discord_button.setEnabled(True)
        if not community.is_discord_invite(url):
            url = community.DISCORD_FALLBACK  # never open anything that is not an invite
        QDesktopServices.openUrl(QUrl(str(url)))

    def _open_config_file(self) -> None:
        if self._config is None:
            return
        self._open_path(str(self._config.path), "config file")

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

    def _form_state(self) -> dict[str, Any]:
        """The form's current values, for comparing against the loaded ones."""
        state: dict[str, Any] = {
            "title": self.title.text(),
            "background": self.background.text(),
            "remember": self.remember.isChecked(),
            "language": self.language.currentData(),
            "check_updates": self.check_updates.isChecked(),
            "check_notices": self.check_notices.isChecked(),
            "notify_when_done": self.notify_when_done.isChecked(),
            "hide_uninstalled": self.hide_uninstalled.isChecked(),
            "default_monitor": self.default_monitor.currentData(),
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
        self.note.setText(f"Fetched {count_text(len(paths), 'image')} to {Path(cover).parent}")

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
        # Under the config lock: a worker saving meanwhile must not resize the dict mid-copy.
        profiles: dict = {}
        if self._config is not None:
            with self._config.locked():
                profiles = copy.deepcopy(self._config.profiles)
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
        elif index < 0 and selected and _is_runner_folder(selected):
            # A Proton tree of the user's own (not in Nadare's list) works: say so (#331 F-10).
            self.runner.insertItem(0, f"Custom · {Path(selected).expanduser().name}", selected)
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
            row = QHBoxLayout()
            row.addWidget(self.move_button)
            row.addWidget(self.relink_button)
            row.addStretch(1)
            layout.addLayout(row)
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
        if kind == "monitor":
            editor = QComboBox()
            _fill_monitor_combo(editor)
            return editor, editor
        if kind == "runner":
            return self.runner, self.runner
        editor = QLineEdit()
        return editor, editor

    #: Shown under the locked prefix field (the note is reworded while a check is pending/failed).
    _LOCKED_NOTE = (
        "The game is installed, so this location is locked. Editing it here would make "
        "Nadare would no longer find the game. Use the Move button to relocate it, or "
        "'I moved it myself' if you already did."
    )

    def _refresh_prefix_lock(self, has_wine_prefix: bool) -> None:
        """Lock the prefix now, then unlock it only if the game is shown not to be installed.

        The status probe can be slow (keyring, ``/proc`` scan), so it runs on the thread pool; a
        failed probe leaves the field locked, since unlocking is the risky direction.
        """
        self._prefix_lock_generation += 1
        self._game_idle = False  # unknown until this profile's probe answers
        self._probed_prefix = ""
        if not has_wine_prefix or self._config is None or not self._profile:
            self._set_prefix_locked(False)
            return
        self._set_prefix_locked(True, "Checking whether the game is installed…")
        generation, profile, config = self._prefix_lock_generation, self._profile, self._config

        def probe() -> tuple[int, object]:
            try:
                return generation, create_backend(profile, config).status()
            except Exception:  # noqa: BLE001 - reported as "could not check", never as "free"
                return generation, None

        task = CallTask(probe)
        # A bound method, so Qt drops the connection if the page is destroyed meanwhile.
        task.signals.done.connect(self._on_prefix_probe)
        QThreadPool.globalInstance().start(task)

    def _on_prefix_probe(self, answer: object) -> None:
        """Apply a finished status probe on the GUI thread (a stale answer is dropped)."""
        generation, status = answer  # type: ignore[misc]
        if generation != self._prefix_lock_generation:
            return
        if status is None:
            self._game_idle = False
            self._set_prefix_locked(
                True,
                "Could not check whether the game is installed, so this location stays locked.",
            )
            return
        state = getattr(status, "state", "")
        self._game_idle = state != "running"
        data = getattr(status, "data", None)
        # A profile with no prefix key uses a folder of Nadare's own; the status knows where.
        self._probed_prefix = str(data.get("prefix") or "") if isinstance(data, dict) else ""
        self._set_prefix_locked(state in ("installed", "running"))

    def _set_prefix_locked(self, locked: bool, note: str | None = None) -> None:
        """Stop an installed game's prefix from being silently repointed."""
        editor = self._option_widgets.get("prefix")
        if isinstance(editor, QLineEdit):
            editor.setReadOnly(locked)
        browse = self._option_browse.get("prefix")
        if browse is not None:
            browse.setEnabled(not locked)
        if self._prefix_lock_note is not None:
            self._prefix_lock_note.setText(note or self._LOCKED_NOTE)
            self._prefix_lock_note.setVisible(locked)
        self.relink_button.setVisible(locked)
        self.relink_button.setEnabled(locked and self._game_idle)
        self.move_button.setVisible(locked)
        self.move_button.setEnabled(locked and self._game_idle and not self._moving)
        self.move_button.setText(self._move_label())
        self.move_button.setToolTip(
            "Available once Nadare has confirmed the game is not running."
            if not self._game_idle
            else self._move_tip()
        )
        self.relink_button.setToolTip(
            "Available once Nadare has confirmed the game is not running."
            if not self._game_idle
            else "Use this after moving the game's Wine prefix folder yourself. "
            "Nothing is copied or deleted."
        )

    def _first_deletable_backup(self, name: str) -> Path | None:
        """The first recorded backup the removal dialog would also let the user delete."""
        if self._config is None or name not in self._config.profiles:
            return None
        from ..core import removal

        for tree in removal.removable_trees(self._config, name):
            if tree.key.startswith("prefix_backup") and tree.safe:
                return tree.path
        return None

    def _refresh_backup_row(self, data: dict[str, Any] | None = None) -> None:
        """Show the first recorded rebuild backup that can really be deleted, if any."""
        if self._backup_label is None or self._backup_button is None:
            return
        found = self._first_deletable_backup(self._profile)
        self._backup_label.setText(f"Old prefix backup: {found}" if found else "")
        self._backup_label.setVisible(found is not None)
        self._backup_button.setVisible(found is not None)

    def _delete_old_backup(self) -> None:
        """Delete one recorded old prefix, after confirmation, from the prefix field."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        target = self._first_deletable_backup(name)
        if target is None:
            self._refresh_backup_row()
            return
        answer = QMessageBox.question(
            self,
            "Delete old backup",
            f"Permanently delete the old prefix backup at {target}?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from ..core import prefixmove

        try:
            prefixmove.delete_backup(config, name, target)
        except (prefixmove.PrefixMoveError, OSError) as exc:
            QMessageBox.warning(self, "Delete old backup", str(exc))
            return
        self.note.setText(f"Deleted old prefix backup {target}")
        self._refresh_backup_row()

    def _make_relink_button(self) -> QPushButton:
        button = QPushButton("I moved it myself — use the new location…")
        button.setAutoDefault(False)
        button.setVisible(False)
        button.clicked.connect(self._relink_prefix)
        return button

    def _relink_prefix(self) -> None:
        """Point the profile at a prefix the user moved by hand (nothing is copied)."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles or not self._game_idle:
            return
        data = config.profiles[name]
        start = str(data.get("prefix") or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select the moved Wine prefix", start)
        if not chosen:
            return
        try:
            plan = relink_core.plan_relink(data, chosen, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Wine prefix", str(exc))
            return
        message = (
            f"Nadare will use {plan.changes['prefix']} as this game's Wine prefix from now on. "
            "Nothing is copied, moved or deleted."
        )
        details = [*plan.notes, *plan.warnings]
        if details:
            message += "\n\n" + "\n".join(details)
        answer = QMessageBox.question(self, "Use the new location?", message)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if plan.sibling_install:
            follow = QMessageBox.question(
                self,
                "Game folder",
                f"The game folder seems to be at {plan.sibling_install} now. "
                "Use it as this game's folder too?",
            )
            if follow == QMessageBox.StandardButton.Yes:
                plan = relink_core.follow_install(data, plan, plan.sibling_install)
        # The page's idea of "not running" may be minutes old, or the game may have been started
        # outside Nadare since: ask once more, off the GUI thread, and only then save.
        self.relink_button.setEnabled(False)
        self._relink_ticket += 1
        ticket = self._relink_ticket
        self._pending_relink[ticket] = (config, name, plan, chosen)

        def probe() -> object:
            try:
                return ticket, getattr(create_backend(name, config).status(), "state", "")
            except Exception:  # noqa: BLE001 - unknown means do not relink
                return ticket, None

        task = CallTask(probe)
        # A bound method, so Qt drops the connection if the page is destroyed meanwhile.
        task.signals.done.connect(self._on_relink_probe)
        QThreadPool.globalInstance().start(task)

    def _on_relink_probe(self, answer: object) -> None:
        """Finish a relink once the game's state is known right now."""
        ticket, state = answer if isinstance(answer, tuple) else (None, answer)
        pending = self._pending_relink.pop(ticket, None)
        if pending is None:
            return
        config, name, plan, chosen = pending
        if state is None:
            self.relink_button.setEnabled(self._game_idle)
            QMessageBox.warning(
                self,
                "Wine prefix",
                "Could not check whether the game is running, so Nadare did not change where "
                "its Wine prefix is.",
            )
            return
        on_page = config is self._config and name == self._profile
        if state == "running":
            if on_page:
                self._game_idle = False
                self._set_prefix_locked(True)  # the buttons follow: both stay off while it runs
            QMessageBox.warning(self, "Wine prefix", "The game is running. Close it first.")
            return
        self.relink_button.setEnabled(self._game_idle)
        lock = process_core.OperationLock(name)
        try:
            lock.acquire()
        except process_core.ProfileLockError:
            QMessageBox.warning(self, "Wine prefix", process_core.OPERATION_BUSY.format(name=name))
            return
        try:
            if self._commit_relink(config, name, plan):
                self.note.setText(f"Now using {chosen}")
        finally:
            lock.release()

    def _commit_relink(self, config: Config, name: str, plan: relink_core.Relink) -> bool:
        """Save ``plan`` into profile ``name``; False if it could not be saved.

        The profile is passed in, never read from the page: a move finishes minutes after it
        started and the page may show another game, or no page at all, by then.
        """
        if name not in config.profiles:
            return False
        alive = not sip.isdeleted(self)
        data = config.profiles[name]
        backup = copy.deepcopy(data)
        libraries_backup = copy.deepcopy(config.data.get(core_libraries.CONFIG_KEY))
        try:
            with config.locked():
                relink_core.apply(data, plan)
                # The manifest moved with the folder; refresh it and remember the new library,
                # only when the game's folder itself moved (a library the user removed must not
                # come back because a prefix was relinked).
                core_libraries.sync_game(
                    config, name, remember_library=core_libraries.folder_moved(backup, data)
                )
                config.save()
        except OSError as exc:
            with config.locked():
                data.clear()
                data.update(backup)
                if libraries_backup is None:
                    config.data.pop(core_libraries.CONFIG_KEY, None)
                else:
                    config.data[core_libraries.CONFIG_KEY] = libraries_backup
            QMessageBox.critical(
                self if alive else None, "Settings", f"Could not save config: {exc}"
            )
            return False
        if alive:
            if config is self._config and name == self._profile:
                editor = self._option_widgets.get("prefix")
                if isinstance(editor, QLineEdit):
                    editor.setText(str(plan.changes["prefix"]))
                if "install_dir" in plan.changes:
                    self.install_dir.setText(str(plan.changes["install_dir"]))
                self._loaded_state = self._form_state()
            self.saved.emit(name)
        return True

    def show_move_section(self) -> None:
        """Switch to the tab that holds the Move button."""
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).isAncestorOf(self.move_button):
                self.tabs.setCurrentIndex(index)
                return

    def _make_move_button(self) -> QPushButton:
        button = QPushButton("Move to another folder…")
        button.setAutoDefault(False)
        button.setVisible(False)
        button.clicked.connect(self._move_prefix)
        return button

    def _managed_home(self) -> Path | None:
        config, name = self._config, self._profile
        data = config.profiles.get(name) if config is not None and name else None
        return relink_core.managed_home(data) if isinstance(data, dict) else None

    def _move_label(self) -> str:
        return "Move game and prefix…" if self._managed_home() else "Move prefix only…"

    def _move_tip(self) -> str:
        if self._managed_home():
            return (
                "Move the game folder and its Wine prefix together into a library folder you "
                "pick. Nadare keeps your original if it has to copy to another disk."
            )
        return (
            "Move only the Wine prefix to another folder; the game folder stays where it is. "
            "Nadare keeps your original if it has to copy to another disk."
        )

    def _move_prefix(self) -> None:
        """Move the prefix (and the game folder inside it) to a folder the user picks."""
        config, name = self._config, self._profile
        if config is None or not name or name not in config.profiles:
            return
        if not self._game_idle or self._moving:
            return
        self._moving = True  # also covers the dialogs below: a second click must not start a move
        self.move_button.setEnabled(False)
        if not self._ask_and_run_move(config, name):
            self._moving = False
            self.move_button.setEnabled(self._game_idle)

    def _ask_and_run_move(self, config: Config, name: str) -> bool:
        """Check, ask, then start the move; True once it is running."""
        data = config.profiles[name]
        if not data.get("prefix"):
            where = (
                f"Nadare is using its own folder, {self._probed_prefix}. "
                if self._probed_prefix
                else ""
            )
            QMessageBox.warning(
                self,
                "Wine prefix",
                "This game has no prefix set, so there is nothing to move. "
                f"{where}Use \"I moved it myself\" and pick that folder to give the game a "
                "location of its own, then Move will work.",
            )
            return False
        old = Path(str(data.get("prefix"))).expanduser()
        if not old.is_dir():
            QMessageBox.warning(
                self,
                "Wine prefix",
                f"{old} was not found. If you moved it yourself, use "
                "\"I moved it myself\" instead.",
            )
            return False
        try:
            relink_core.check_movable(data, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Wine prefix", str(exc))
            return False
        home = relink_core.managed_home(data)
        if home is not None:
            return self._ask_and_move_home(config, name, home)
        parent = QFileDialog.getExistingDirectory(
            self, "Move the Wine prefix into which folder?", str(old.parent)
        )
        if not parent:
            return False
        if Path(parent).resolve() == old.parent.resolve():
            QMessageBox.warning(
                self,
                "Wine prefix",
                "The prefix is already in that folder. Pick a different folder.",
            )
            return False
        dst = relink_core.prefix_destination(data, old, parent)
        message = (
            f"Move {old} to {dst}?\n\nOn the same disk this is instant. On another disk "
            "Nadare copies it, checks the copy, and keeps your original, so you can delete "
            "it yourself when you are happy. Nothing is deleted."
        )
        install = str(data.get("install_dir") or "")
        if install and not relink_core.is_inside(Path(install).expanduser(), old):
            message += f"\n\nOnly the Wine prefix moves; the game folder stays at {install}."
        answer = QMessageBox.question(self, "Move the Wine prefix?", message)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._run_move(config, name, old, dst)
        return True

    def _ask_and_move_home(self, config: Config, name: str, home: Path) -> bool:
        """Move a game Nadare laid out as ``<library>/Nadare/<Game>`` as one unit."""
        data = config.profiles[name]
        title = data.get("title") or name
        library = QFileDialog.getExistingDirectory(
            self, f"Move {title} into which folder?", str(home.parent.parent)
        )
        if not library:
            return False
        try:
            relink_core.check_home_target(data, library, name=name, profiles=config.profiles)
        except relink_core.RelinkError as exc:
            QMessageBox.warning(self, "Move the game", str(exc))
            return False
        dst = relink_core.free_home(library, relink_core.home_name(data, home), config.profiles)
        install = str(data.get("install_dir") or "")
        install_exists = bool(install) and Path(install).expanduser().is_dir()
        text = (
            f"Move {home} to {dst}?\n\nThe game folder and its Wine prefix move together. "
            "On the same disk this is instant. On another disk Nadare copies them, checks the "
            "copy, and keeps your original, so you can delete it yourself when you are happy. "
            "Nothing is deleted."
        )
        if install and not install_exists:
            text += (
                "\n\nThe game folder does not exist yet (the game's own launcher installs it), "
                f"so only the Wine prefix is copied now; the game will install into {dst}."
            )
        answer = QMessageBox.question(self, "Move the game?", text)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._run_move(config, name, home, dst, managed=True, install_existed=install_exists)
        return True

    def _run_move(
        self,
        config: Config,
        name: str,
        old: Path,
        dst: Path,
        *,
        managed: bool = False,
        install_existed: bool = True,
    ) -> None:
        from PyQt6.QtWidgets import QProgressDialog

        from ..core.events import Cancel

        if self._move_running:
            return
        self._move_running = True
        cancel = Cancel()
        signals = _MoveSignals(self)
        dialog = QProgressDialog(
            "Moving the game and prefix…" if managed else "Moving the Wine prefix…",
            "Cancel",
            0,
            100,
            self,
        )
        dialog.setWindowTitle("Moving")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancel.cancel)

        def on_progress(done: object, total: object) -> None:
            total_bytes = max(int(total), 1)  # type: ignore[call-overload]
            dialog.setValue(min(99, int(int(done) * 100 / total_bytes)))  # type: ignore[call-overload]

        signals.progress.connect(on_progress)
        self._moving = True
        self.move_button.setEnabled(False)

        # The lock outlives the worker: the profile names the old path until done() saves the
        # new one, and another window or the CLI must not work on it in between (#971).
        held: list[process_core.OperationLock] = []

        def release_held() -> None:
            while held:
                held.pop().release()

        def work() -> object:
            lock = process_core.OperationLock(name)
            try:
                lock.acquire()
            except process_core.ProfileLockError:
                raise relocate_core.RelocateError(
                    process_core.OPERATION_BUSY.format(name=name)
                ) from None
            moved = False
            try:
                # The page's idea of "not running" may be minutes old: ask again right now.
                try:
                    state = getattr(create_backend(name, config).status(), "state", "")
                except Exception as exc:  # noqa: BLE001 - unknown means do not move
                    raise relocate_core.RelocateError(
                        f"Could not check whether the game is running ({exc})."
                    ) from None
                if state == "running":
                    raise relocate_core.RelocateError(
                        "The game is running. Close it first."
                    )
                result = relocate_core.move_tree(
                    old, dst, cancel=cancel, progress=lambda d, t: signals.progress.emit(d, t)
                )
                moved = True
                held.append(lock)
                return result
            finally:
                if not moved:
                    lock.release()

        task = CallTask(work)

        def finish() -> bool:
            """Reset the state; False when the page is gone (nothing on it may be touched)."""
            self._moving = False
            self._move_running = False
            if sip.isdeleted(self):
                return False
            dialog.canceled.disconnect()  # closing the dialog would otherwise count as Cancel
            dialog.close()
            dialog.deleteLater()
            signals.deleteLater()
            self.move_button.setEnabled(self._game_idle)
            return True

        def done(result: object) -> None:
            try:
                _done(result)
            finally:
                release_held()

        def _done(result: object) -> None:
            alive = finish()
            parent = self if alive else None
            if name not in config.profiles:
                QMessageBox.warning(
                    parent,
                    "Wine prefix",
                    f"The game was removed while it moved. Its folder is now {dst}.",
                )
                return
            stuck = (
                f"Nadare moved the folder to {dst} but could not update the game. Use "
                "\"I moved it myself\" in the game's Settings and pick "
                f"{dst / 'prefix' if managed else dst}."
            )
            try:
                if managed:
                    plan = relink_core.plan_home_move(
                        config.profiles[name], dst, install_existed=install_existed
                    )
                else:
                    plan = relink_core.plan_relink(
                        config.profiles[name], dst, made_by_nadare=True
                    )
            except relink_core.RelinkError as exc:
                QMessageBox.warning(parent, "Wine prefix", f"{exc}\n\n{stuck}")
                return
            if not self._commit_relink(config, name, plan):
                QMessageBox.warning(parent, "Wine prefix", stuck)
                return
            if not alive:
                return
            kept = getattr(result, "kept_old", None)
            if kept is not None:
                QMessageBox.information(
                    self,
                    "Moved",
                    f"Now using {dst}.\n\nYour original is still at {kept}. Delete it "
                    "yourself when you are happy with the new one; Nadare will not.",
                )
            self.note.setText(f"Moved to {dst}")

        def failed(message: str) -> None:
            release_held()  # normally already released by the worker
            if not finish():
                return
            if cancel.cancelled:
                self.note.setText("Move cancelled; nothing was changed.")
                return
            QMessageBox.warning(
                self, "Wine prefix", f"{message}\n\nNothing was changed."
            )

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

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

    # ---- load / save -------------------------------------------------------
    def load(self, config: Config, profile: str) -> None:
        """Populate the form from ``config`` for ``profile``."""
        self._config = config
        self._profile = profile
        self._set_app_only(False)
        self._pending_art = {}
        self.tabs.setCurrentIndex(0)
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
        self._refresh_backup_row(data)
        if self._files_form is not None:
            self._files_form.setRowVisible(
                self._remake_prefix_row, kind in runners_core.PROTON_DEFAULT_BACKENDS
            )
        has_wine_prefix = any(
            option.key == "prefix" and option.label == "Wine prefix"
            for option in self._backend_options(kind)
        )
        self._refresh_prefix_lock(has_wine_prefix)

        supported = supports_remember_me(str(data.get("backend") or ""))
        self.account_card.setVisible(supported)
        self.remember.setChecked(bool(data.get("remember_me", False)))

        self._load_app_values(config)
        self.note.setText(f"Saved in {config.path}")
        self._loaded_state = self._form_state()

    def _load_app_values(self, config: Config) -> None:
        self.libraries_panel.load(config)
        """Fill the controls that belong to Nadare itself, not to one game."""
        language = str(config.data.get("language") or i18n.DEFAULT_LANGUAGE)
        lang_index = self.language.findData(language)
        self.language.setCurrentIndex(lang_index if lang_index >= 0 else 0)
        self.check_updates.setChecked(bool(config.data.get("check_updates", True)))
        self.check_notices.setChecked(bool(config.data.get("check_notices", True)))
        self.notify_when_done.setChecked(bool(config.data.get("notify_when_done", True)))
        self.hide_uninstalled.setChecked(bool(config.data.get("sidebar_hide_uninstalled", False)))
        saved_monitor = config.data.get("default_monitor")
        _fill_monitor_combo(self.default_monitor, saved_monitor)
        monitor_index = self.default_monitor.findData(str(saved_monitor or ""))
        self.default_monitor.setCurrentIndex(monitor_index if monitor_index >= 0 else 0)

    def _set_app_only(self, app_only: bool) -> None:
        self._app_only = app_only
        self.subtitle.setText(APP_SUBTITLE if app_only else GAME_SUBTITLE)
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, not app_only or index == self._app_tab)
        for button in (self.remove_button, self.report_button, self.logs_button):
            button.setVisible(not app_only)

    def load_app_only(self, config: Config) -> None:
        """Show only Nadare's own settings, for when no game is open."""
        self._config = config
        self._profile = ""
        self._set_app_only(True)
        self._pending_art = {}
        self.tabs.setCurrentIndex(self._app_tab)
        self.heading.setText("Nadare settings")
        self._load_app_values(config)
        self.note.setText(f"Saved in {config.path}")
        self._loaded_state = self._form_state()

    def _save_app_only(self) -> None:
        config = self._config
        keys = (
            "language",
            "check_updates",
            "check_notices",
            "notify_when_done",
            "sidebar_hide_uninstalled",
            "default_monitor",
        )
        try:
            with config.locked():
                backup = {key: config.data.get(key) for key in keys}
                config.data["language"] = self.language.currentData() or i18n.DEFAULT_LANGUAGE
                config.data["check_updates"] = bool(self.check_updates.isChecked())
                config.data["check_notices"] = bool(self.check_notices.isChecked())
                config.data["notify_when_done"] = bool(self.notify_when_done.isChecked())
                config.data["sidebar_hide_uninstalled"] = bool(self.hide_uninstalled.isChecked())
                if self.default_monitor.currentData():
                    config.data["default_monitor"] = self.default_monitor.currentData()
                else:
                    config.data.pop("default_monitor", None)
                config.save()
        except OSError as exc:
            with config.locked():
                for key, old in backup.items():
                    if old is None:
                        config.data.pop(key, None)
                    else:
                        config.data[key] = old
            QMessageBox.critical(self, "Settings", f"Could not save config: {exc}")
            return
        i18n.set_language(config.data["language"])
        self._loaded_state = self._form_state()
        self.saved.emit("")

    def _confirm_environment(self, data: dict[str, Any]) -> bool:
        """Ask before saving a changed, non-empty Environment; True means go ahead.

        A wrong variable can stop a game launching, so a change needs a deliberate
        yes. Unchanged or cleared text never asks.
        """
        editor = self._option_widgets.get("extra_env")
        if editor is None:
            return True
        new = _text_to_env(editor.toPlainText())
        old = self._option_value(data, "extra_env")
        if not new or new == (old if isinstance(old, dict) else {}):
            return True
        answer = QMessageBox.question(
            self,
            "Change Environment?",
            "These variables change how the game starts, and a wrong one can stop it "
            "from launching. Keep them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _unsafe_folder_message(self, data: dict[str, Any]) -> str:
        """Why a changed Wine prefix or game folder cannot be saved, or ``""`` (#851).

        Only a value the user just changed is judged, so a folder saved earlier does not block
        saving other settings.
        """
        from ..core import prefixguard

        for key in ("prefix", "wineprefix", "install_dir"):
            editor = self._option_widgets.get(key)
            if editor is None or not hasattr(editor, "text"):
                continue
            value = editor.text().strip()
            if not value or value == str(data.get(key) or "").strip():
                continue
            reason = prefixguard.unsafe_reason(
                value,
                profiles=self._config.profiles if self._config is not None else None,
                own=self._profile,
            )
            if reason:
                return prefixguard.explain(value, reason)
        return ""

    def save(self) -> None:
        if self._config is not None and self._app_only:
            self._save_app_only()
            return
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

        refusal = self._unsafe_folder_message(data)
        if refusal:
            QMessageBox.critical(self, "Settings", refusal)
            return

        runner_text = self.runner.currentText().strip()
        index = self.runner.currentIndex()
        if index >= 0 and runner_text == self.runner.itemText(index):
            runner_value = self.runner.currentData()
        else:
            # The user typed a custom runner path/tag.
            runner_value = runner_text

        if not self._confirm_environment(data):
            return
        if self._refuse_broken_values(backend, runner_text, index):
            return

        # The mutation and the write are taken under the config lock (as the
        # worker-side ``BackendContext.set`` does); otherwise a worker save can
        # serialise this profile dict while it is being changed.
        old_quality = self._option_value(data, "wuwa_quality")
        import copy as _copy

        with config.locked():
            backup = (
                _copy.deepcopy(data),
                config.data.get("language"),
                config.data.get("check_updates"),
                config.data.get("check_notices"),
                config.data.get("notify_when_done"),
                config.data.get("sidebar_hide_uninstalled"),
                config.data.get("default_monitor"),
            )
        warnings: list[str] = []
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
                config.data["check_notices"] = bool(self.check_notices.isChecked())
                config.data["notify_when_done"] = bool(self.notify_when_done.isChecked())
                config.data["sidebar_hide_uninstalled"] = bool(self.hide_uninstalled.isChecked())
                if self.default_monitor.currentData():
                    config.data["default_monitor"] = self.default_monitor.currentData()
                else:
                    config.data.pop("default_monitor", None)
                for kind, path in self._pending_art.items():
                    artwork.record_thirdparty(data, path, kind)
                config.save()
        except OSError as exc:
            with config.locked():
                data.clear()
                data.update(backup[0])
                for key, old in zip(
                    (
                        "language",
                        "check_updates",
                        "check_notices",
                        "notify_when_done",
                        "sidebar_hide_uninstalled",
                        "default_monitor",
                    ),
                    backup[1:],
                    strict=True,
                ):
                    if old is None:
                        config.data.pop(key, None)
                    else:
                        config.data[key] = old
            QMessageBox.critical(self, "Settings", f"Could not save config: {exc}")
            return
        self._pending_art = {}
        i18n.set_language(config.data["language"])
        self.note.setText(f"Saved to {config.path}")
        from ..core.sources import wuwa

        new_quality = self._option_value(data, "wuwa_quality")
        old_tier = wuwa.quality_of(old_quality)
        new_tier = wuwa.quality_of(new_quality)
        quality_note = ""
        if old_tier != new_tier:
            quality_note = (
                f"The next Update downloads the {new_tier.upper()} files; "
                f"Nadare offers to remove the {old_tier.upper()} files once that is done."
            )
        self._loaded_state = self._form_state()
        self.saved.emit(self._profile)
        if warnings:
            if quality_note:
                warnings.append(quality_note)
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
        elif quality_note:
            # The page has already closed, so the side status is where this can be read.
            self.notice.emit(self._profile, quality_note)


__all__ = [
    "SettingsPage",
    "discover_runners",
    "SYSTEM_WINE",
    "INT_RANGES",
    "DEFAULT_INT_RANGE",
]
