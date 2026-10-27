# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The window shell: a sidebar plus library, per-game and settings pages.

``MainWindow`` is the only object that knows about both the UI and the backend
controller. It never calls a backend method directly — every request goes through
:class:`~nadare.gui.workers.BackendController`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

from PyQt6.QtCore import (
    QByteArray,
    QEvent,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import backends as core_backends
from ..core import diskspace, paths, secrets, shortcuts
from ..core import runners as core_runners
from ..core.backends import (
    Status,
    supports_login,
    supports_media,
    supports_news,
    supports_remember_me,
)
from ..core.config import Config
from ..core.directkinds import DIRECT_KINDS
from ..core.events import Event
from ..core.news import NewsResult
from . import i18n
from .add_game import AddGameDialog
from .banner import InfoBanner
from .game_view import OPERATION_TITLES, GameSnapshot, GameView
from .library import LibraryPage, count_text, profile_title
from .notice import AccountNoticeDialog
from .settings import SettingsPage
from .sidebar import SidebarGames
from .store_flow import StoreFlow
from .theme import app_icon, apply_theme, brand_pixmap, repolish
from .workers import BackendController, CallTask

PAGE_LIBRARY = 0
PAGE_GAME = 1
PAGE_SETTINGS = 2
#: Appended last so the older page indexes never move.
PAGE_STORE = 3

#: Operations that must not run until the account notice is acknowledged.
NOTICE_OPERATIONS = frozenset({"install", "update", "launch"})

#: Operations before which the one-time "unofficial launcher" notice is shown.
UNOFFICIAL_OPERATIONS = frozenset({"install", "login", "launch"})

#: A fresh install larger than this asks for confirmation first.
DOWNLOAD_CONFIRM_BYTES = 1 << 30

#: A snapshot younger than this skips the news/media refetch on a switch.
#: ``status`` always refreshes, but silently.
SNAPSHOT_TTL = 5 * 60
#: Events kept per running operation so a game page can replay its log.
_REPLAY_EVENTS = 400

#: Background refreshes that must never put the page into the busy state.
SILENT_OPERATIONS = frozenset({"status", "news", "media", "check_update"})

#: How long before a game page asks for the published version again (6 hours).
UPDATE_CHECK_INTERVAL = 6 * 60 * 60

#: How often a visible window re-probes the game page's status (#371).
STATUS_REFRESH_INTERVAL_MS = 30_000
#: Ceiling for the retry backoff after consecutive failed status probes of one game.
STATUS_ERROR_MAX_INTERVAL_MS = 10 * 60_000


def _unknown_backend_message(value: str) -> str:
    """Plain explanation for an unknown/empty ``backend`` value (#366)."""
    return (
        f"This game's type ('{value}') is not recognised by this version of Nadare. "
        "Remove the game and add it again, or correct 'backend' in config.json."
    )


def _removal_reason_text(reason: str) -> str:
    """Turn ``core/removal.py``'s internal reasons into plain language (#351 F-15)."""
    text = (reason or "").strip()
    fixed = {
        "is a symlink": "it is a link to another folder",
        "is a mount point": "it is a separate disk or drive",
        "is home or an ancestor of home": "it contains your home folder",
        "is inside nadare's own directories": "it is inside Nadare's own folder",
    }
    if text in fixed:
        return fixed[text]
    if text.startswith("shared with "):
        return f"another game ({text.removeprefix('shared with ')}) also uses it"
    return text or "Nadare cannot safely delete it"

#: Busy operations that cannot stop mid-flight, so their button must not offer CANCEL.
UNCANCELLABLE_OPERATIONS = frozenset({"verify"})

#: Failures after which the install state may have changed: probe it again.
REPROBE_OPERATIONS = frozenset({"install", "update", "launch", "verify"})

log = logging.getLogger(__name__)


#: Side-status wording for operations whose name reads badly as-is.
_OPERATION_LABELS = {"set_remember": "Saving"}


class MainWindow(QMainWindow):
    """Nadare's main window."""

    #: A newer release was found by the background update check.
    update_found = pyqtSignal(object)
    notices_found = pyqtSignal(object)
    #: Unused old runners measured in the background: a list of ``(path, bytes)``.
    runner_cleanup_ready = pyqtSignal(object)
    runner_cleanup_done = pyqtSignal(int, int, int)  # deleted, bytes freed, failed

    def __init__(
        self, config: Config, profile: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._current_profile = ""
        self._busy: dict[str, str] = {}
        #: Set when the window was closed while a game ran: Nadare stays alive,
        #: hidden, and quits once the last game exits.
        self._quit_after_games = False
        #: True once the window is really closing: callbacks must not start new work.
        self._closing = False
        #: A SIGTERM/SIGINT close skips the "still installing" question.
        self._close_requested_by_signal = False
        #: Last rendered state per profile (status/news/media + monotonic stamp).
        self._snapshots: dict[str, dict[str, Any]] = {}
        #: Consecutive failed status probes, used for per-profile retry backoff.
        self._status_errors: dict[str, int] = {}
        #: Bumped whenever a profile disappears or reappears, so a result from a
        #: worker started for the old profile cannot land on its same-named
        #: replacement.
        self._generations: dict[str, int] = {}
        #: Generation each profile had when its current worker was started.
        self._worker_generations: dict[str, int] = {}
        self._known_profiles: set[str] = set(config.profiles)
        #: Runner folders present when the last install/update/launch started, so a
        #: runner it downloaded can be noticed afterwards.
        self._runners_before: set[str] | None = None
        #: Profiles whose pending ``set_remember`` notice must reach the user.
        self._remember_watching: set[str] = set()
        self._remember_notice: dict[str, str] = {}
        #: Monotonic time each profile last asked for its published version.
        self._update_checked: dict[str, float] = {}
        #: Profiles whose Remember-me change arrived while a worker owned them.
        self._remember_pending: set[str] = set()

        # A hand-edited/imported language must never be used to build a path;
        # only codes with a real catalogue are accepted.
        language = str(config.data.get("language") or "en")
        if language not in i18n.available():
            language = i18n.DEFAULT_LANGUAGE
        i18n.set_language(language)
        apply_theme(self)
        self.setWindowTitle(i18n.tr("Nadare"))
        self.setWindowIcon(app_icon())
        self.setMinimumSize(880, 560)  # measured: the game page needs 880 wide
        self._restore_geometry()

        self._build_shell()

        self.store.install_requested.connect(lambda kind: self._add_game_dialog(kind=kind))
        self.store.own_game_requested.connect(lambda: self._add_game_dialog(advanced=True))

        self.controller = BackendController(config, self)
        self._wire_controller()
        self.update_found.connect(self._show_update)
        self.notices_found.connect(self._show_notice)
        self.runner_cleanup_ready.connect(self._offer_runner_cleanup)
        self.runner_cleanup_done.connect(self._runner_cleanup_finished)
        self._start_update_check()
        self._start_notices_check()

        self.refresh_library()
        if profile and profile in self.config.profiles:
            self.open_game(profile)
        elif self.config.active in self.config.profiles:
            self.open_game(self.config.active)
        else:
            self.show_library()
        self._probe_all()

        # Re-check the open game while the window is visible (#371); stopped
        # while hidden by showEvent/hideEvent.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_REFRESH_INTERVAL_MS)
        self._status_timer.timeout.connect(self._on_status_timer)

    # ---- window activation -------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._on_window_activated()

    def hideEvent(self, event: Any) -> None:
        self._status_timer.stop()
        super().hideEvent(event)

    def _on_window_activated(self) -> None:
        """Catch up with other processes and re-probe the shown game (#371, #376)."""
        self._sync_profiles_from_disk()
        self._refresh_status_if_idle()

    def _on_status_timer(self) -> None:
        """Re-probe the shown game, backing off while its probes keep failing."""
        if not self.isVisible():
            return
        profile = self._current_profile
        if not profile or profile not in self.config.profiles or profile in self._busy:
            return
        if self._status_errors.get(profile, 0):
            checked_at = self._snapshots.get(profile, {}).get("status_at")
            wait = self._status_interval_ms(profile) / 1000
            if checked_at is not None and time.monotonic() - checked_at < wait:
                return
        self._start(profile, "status", silent=True)

    def _status_interval_ms(self, profile: str) -> int:
        """Polling interval: the normal refresh, or an exponential retry after failures."""
        failures = self._status_errors.get(profile, 0)
        if not failures:
            return STATUS_REFRESH_INTERVAL_MS
        return min(60_000 * 2 ** min(failures - 1, 4), STATUS_ERROR_MAX_INTERVAL_MS)

    def _sync_profiles_from_disk(self) -> None:
        """Drop profiles another process removed; keep unsaved local additions."""
        disk = self.config.disk_profile_names()
        if disk is None:
            return  # unreadable file: never read that as "everything was removed"
        dropped: list[str] = []
        for name in list(self.config.profiles):
            if name in disk or name in self.config._removed:
                continue
            if name not in self.config._snapshot:
                continue  # added locally and not saved yet
            with self.config.locked():
                self.config.remove_profile(name)
            # Already gone from disk: do not let a later save treat it as ours to
            # delete, nor block another process from re-adding the same name.
            self.config._removed.discard(name)
            self.config._snapshot.pop(name, None)
            self._snapshots.pop(name, None)
            self._status_errors.pop(name, None)
            dropped.append(name)
        if not dropped:
            return
        current_removed = self._current_profile in dropped
        self.refresh_library()
        if current_removed:
            self.show_library()
            self._set_side_status("This game was removed by another Nadare process.")

    def _refresh_status_if_idle(self) -> None:
        """Silently re-probe the shown profile unless a worker owns it (#371)."""
        profile = self._current_profile
        if not profile or profile not in self.config.profiles:
            return
        if profile in self._busy:
            return
        self._start(profile, "status", silent=True)

    # ---- shell -------------------------------------------------------------
    def _build_shell(self) -> None:
        central = QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.library = LibraryPage()
        self.game = GameView()
        self.settings = SettingsPage()
        self.store = StoreFlow()
        self.stack.addWidget(self.library)
        self.stack.addWidget(self.game)
        self.stack.addWidget(self.settings)
        self.stack.addWidget(self.store)
        self.update_banner = InfoBanner()
        self.update_banner.action_clicked.connect(self._open_update_page)
        self.update_banner.dismissed.connect(self._dismiss_update)
        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(0)
        right_column.addWidget(self.update_banner)
        self.notice_banner = InfoBanner()
        self.notice_banner.action_clicked.connect(self._open_notice_link)
        self.notice_banner.dismissed.connect(self._dismiss_notice)
        self._notice = None
        right_column.addWidget(self.notice_banner)
        right_column.addWidget(self.stack, 1)
        root.addWidget(right, 1)

        # UI intent -> controller / navigation.
        self.library.open_game.connect(self.open_game)
        self.library.add_game.connect(self.show_store)
        self.game.back_requested.connect(self.show_library)
        self.game.action_requested.connect(self._on_game_action)
        self.game.cancel_requested.connect(self._on_cancel)
        self.game.remember_toggled.connect(self._on_remember_toggled)
        self.game.settings_requested.connect(self.open_settings)
        self.game.report_requested.connect(self._show_support_report)
        self.settings.saved.connect(self._on_settings_saved)
        self.settings.cancelled.connect(self.show_library)
        self.settings.remove_requested.connect(self._on_remove_requested)
        self.settings.remake_prefix_requested.connect(self._on_remake_prefix_requested)
        self.settings.report_requested.connect(self._show_support_report)
        self.settings.logs_requested.connect(self._show_logs)
        self.sidebar_games.open_game.connect(self.open_game)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(236)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 20)
        side.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(11)
        mark = QFrame()
        mark.setObjectName("BrandMark")
        mark.setFixedSize(36, 36)
        mark_layout = QVBoxLayout(mark)
        mark_layout.setContentsMargins(0, 0, 0, 0)
        self.brand_mark_icon = QLabel()
        self.brand_mark_icon.setObjectName("BrandGlyph")
        self.brand_mark_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_mark_icon.setPixmap(brand_pixmap(24))
        mark_layout.addWidget(self.brand_mark_icon)
        brand_row.addWidget(mark)
        brand = QLabel("NADARE")
        brand.setObjectName("Brand")
        # First public releases are betas: say so where people look first.
        # 雪崩 ("nadare" = avalanche) sits on the same row as the pill.
        self.beta_pill = QLabel("BETA")
        self.beta_pill.setObjectName("Pill")
        self.beta_pill.setProperty("state", "info")
        self.beta_pill.setToolTip("Beta: expect bugs. Please report what you hit.")
        self.brand_cjk = QLabel("雪崩")
        self.brand_cjk.setObjectName("BrandCJK")
        brand_col = QVBoxLayout()
        brand_col.setSpacing(3)
        brand_col.addWidget(brand)
        pill_row = QHBoxLayout()
        pill_row.setSpacing(6)
        pill_row.addWidget(self.brand_cjk)
        pill_row.addWidget(self.beta_pill)
        pill_row.addStretch(1)
        brand_col.addLayout(pill_row)
        brand_row.addLayout(brand_col)
        brand_row.addStretch(1)
        side.addLayout(brand_row)
        # Shown only when the startup check finds a newer release.
        self.update_button = QPushButton()
        self.update_button.setObjectName("GhostButton")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self._open_update_page)
        self._update_url = ""
        self._update_version = ""
        side.addSpacing(8)
        side.addWidget(self.update_button)
        side.addSpacing(26)

        nav_label = QLabel("LIBRARY")
        nav_label.setObjectName("Section")
        side.addWidget(nav_label)
        side.addSpacing(4)

        self.store_button = QPushButton("Store")
        self.store_button.setObjectName("NavItem")
        self.store_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.store_button.clicked.connect(self.show_store)
        side.addWidget(self.store_button)

        self.library_button = QPushButton("Library")
        self.library_button.setObjectName("NavItem")
        self.library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_button.clicked.connect(self.show_library)
        side.addWidget(self.library_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("NavItem")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self.open_settings)
        side.addWidget(self.settings_button)

        side.addSpacing(20)
        self.sidebar_games = SidebarGames()
        side.addWidget(self.sidebar_games, 1)

        divider = QFrame()
        divider.setObjectName("SideDivider")
        divider.setFixedHeight(1)
        side.addWidget(divider)
        side.addSpacing(12)

        status_row = QHBoxLayout()
        status_row.setSpacing(7)
        self._status_dot = QLabel("●")
        self._status_dot.setObjectName("Accent")
        self._status_dot.setAccessibleName("No game selected")
        self._status_dot.setToolTip("No game selected")
        status_row.addWidget(self._status_dot)
        self.side_status = QLabel("No game selected")
        self.side_status.setObjectName("Dim")
        self.side_status.setWordWrap(True)
        self.side_status.setToolTip("No game selected")
        status_row.addWidget(self.side_status, 1)
        side.addSpacing(6)
        side.addLayout(status_row)

        return sidebar

    def _wire_controller(self) -> None:
        self.controller.backend_event.connect(self._on_event)
        self.controller.result.connect(self._on_result)
        self.controller.succeeded.connect(self._on_succeeded)
        self.controller.failed.connect(self._on_failed)
        self.controller.cancelled.connect(self._on_cancelled)
        self.controller.busy.connect(self._on_busy)

    # ---- navigation chrome -------------------------------------------------
    def _set_active_nav(self, active: str) -> None:
        buttons = (
            ("store", self.store_button),
            ("library", self.library_button),
            ("settings", self.settings_button),
        )
        for name, button in buttons:
            button.setProperty("active", "true" if name == active else "false")
            repolish(button)

    def _set_side_status(self, text: str) -> None:
        """Set the sidebar footer, with the full text available as a tooltip."""
        self.side_status.setText(text)
        self.side_status.setToolTip(text)
        self._status_dot.setAccessibleName(text)
        self._status_dot.setToolTip(text)

    def _game_title(self, profile: str) -> str:
        """The human title for ``profile`` (falling back to its key)."""
        return profile_title(profile, self.config.profiles.get(profile) or {})

    # ---- profiles / library ------------------------------------------------
    def _profile_generation(self, profile: str) -> int:
        return self._generations.get(profile, 0)

    def refresh_library(self) -> None:
        """Rebuild the library cards and the sidebar game list from the config."""
        current = set(self.config.profiles)
        for name in self._known_profiles ^ current:
            self._generations[name] = self._profile_generation(name) + 1
        self._known_profiles = current
        self.library.refresh(self.config.profiles)
        for name, snap in self._snapshots.items():
            st = snap.get("status")
            if st is not None and name in self.config.profiles:
                self.library.set_state(name, st.state, st.running)
        self.sidebar_games.set_profiles(self.config.profiles)
        self.store.set_installed_kinds(frozenset(
            str(data.get("backend") or "") for data in self.config.profiles.values()
            if data.get("backend")
        ))

    def _remember_active(self, name: str) -> None:
        """Reopen this game next start (the config's ``active_profile``)."""
        if self.config.active == name:
            return
        try:
            with self.config.locked():
                self.config.set_active(name)
                self.config.save()
        except OSError:
            pass

    def show_library(self) -> None:
        self.stack.setCurrentIndex(PAGE_LIBRARY)
        self._current_profile = ""
        self._set_active_nav("library")
        self.sidebar_games.set_active("")
        self._set_side_status("Library")

    def show_store(self) -> None:
        self.store.show_grid()
        self.stack.setCurrentIndex(PAGE_STORE)
        self._current_profile = ""
        self._set_active_nav("store")
        self.sidebar_games.set_active("")
        self._set_side_status("Store")
        self.store.grid.focus_search()

    def open_game(self, profile: str) -> None:
        """Show the per-game page for ``profile``."""
        if profile not in self.config.profiles:
            self.show_library()
            return
        self._current_profile = profile
        self._remember_active(profile)
        snp = self._snapshots.get(profile, {})
        self.game.set_profile(
            profile,
            self.config.profiles[profile],
            GameSnapshot(
                status=snp.get("status"),
                news=snp.get("news"),
                media=snp.get("media"),
                qr=snp.get("qr"),
            ),
        )
        self.game.set_account_controls(
            supports_login(str(self.config.profiles[profile].get("backend") or ""))
        )
        self.game.set_remember(
            supports_remember_me(str(self.config.profiles[profile].get("backend") or "")),
            bool(self.config.profiles[profile].get("remember_me", False)),
        )
        self.stack.setCurrentIndex(PAGE_GAME)
        self._set_active_nav("library")
        self.sidebar_games.set_active(profile)
        title = self._game_title(profile)
        snap_status = snp.get("status")
        running = bool(
            snap_status and (snap_status.running or snap_status.state == "running")
        )
        self._set_side_status(f"Playing {title}" if running else f"Viewing {title}")
        if profile in self._busy:
            self.game.set_busy(self._busy[profile])
            for past in snp.get("events", ()):
                self.game.append_event(past)
            # A live login QR is a one-shot event; restore it from the snapshot
            # so leaving and returning does not lose the code for the rest of
            # the (up to 300 s) auth run.
            self.game.show_qr(snp.get("qr"))
        else:
            # Status always refreshes, but silently (no busy/CANCEL flash).
            self._start(profile, "status", silent=True)

    def open_settings(self) -> None:
        profile = self._current_profile or self.config.active
        if not profile or profile not in self.config.profiles:
            QMessageBox.information(self, "Settings", "Add a game first.")
            return
        self.settings.load(self.config, profile)
        self.stack.setCurrentIndex(PAGE_SETTINGS)
        self._set_active_nav("settings")
        self.sidebar_games.set_active("")
        self._set_side_status(f"Settings · {self._game_title(profile)}")

    def _show_support_report(self, profile: str) -> None:
        from .support_report import ReportDialog, ReportTask

        if profile not in self.config.profiles:
            profile = self.config.active
        self._report_dialog = ReportDialog(self, profile)
        self._report_dialog.show()
        task = ReportTask(dict(self.config.data), profile)
        task.signals.finished.connect(self._report_dialog.set_report)
        QThreadPool.globalInstance().start(task)

    def _show_logs(self, profile: str) -> None:
        from .logs_dialog import LogsDialog

        if profile not in self.config.profiles:
            profile = self.config.active
        self._logs_dialog = LogsDialog(
            list(self.config.profiles), profile=profile, parent=self
        )
        self._logs_dialog.report_requested.connect(self._show_support_report)
        self._logs_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._logs_dialog.show()

    def _on_remove_requested(self, profile: str) -> None:
        """Confirm, then remove ``profile`` from the library (files kept by default)."""
        if profile not in self.config.profiles:
            return
        if profile in self._busy:
            QMessageBox.information(
                self,
                "Remove game",
                f"{self._game_title(profile)} is busy. Stop it first, then remove it.",
            )
            return
        title = str(self.config.profiles[profile].get("title") or profile)
        data_dir = paths.profiles_dir() / profile
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove game")
        box.setText(f"Remove {title} from Nadare?")
        box.setInformativeText(
            "This removes the game from your library, its shortcuts and its saved login. "
            "Your game files are not deleted."
        )
        delete_data = QCheckBox(
            f"Also delete Nadare's logs and settings for this game ({data_dir}); "
            "game files are only deleted if you tick them below"
        )
        box.setCheckBox(delete_data)
        # Optional: the game's own install folder / Wine prefix, each opt-in and
        # refused (shown disabled with the reason) when it is not safe to delete.
        from ..core import removal

        tree_boxes: dict[str, QCheckBox] = {}
        extra = QWidget()
        extra_layout = QVBoxLayout(extra)
        extra_layout.setContentsMargins(0, 0, 0, 0)
        labels = {
            "install_dir": "game install folder",
            "prefix": "Wine prefix",
            "wineprefix": "Wine prefix",
        }
        for tree in removal.removable_trees(self.config, profile):
            label = f"Also delete the {labels.get(tree.key, tree.key)} ({tree.path})"
            check = QCheckBox(label)
            if not tree.safe:
                why = _removal_reason_text(tree.reason)
                check.setEnabled(False)
                check.setToolTip(f"Not deleted: {why}")
                check.setText(f"{label} - kept: {why}")
            tree_boxes[tree.key] = check
            extra_layout.addWidget(check)
        if tree_boxes:
            layout = box.layout()
            if layout is not None:
                layout.addWidget(extra, layout.rowCount(), 0, 1, layout.columnCount())
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.button(QMessageBox.StandardButton.Yes).setText("Remove")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        chosen = [key for key, check in tree_boxes.items() if check.isChecked()]
        if chosen:
            paths_text = "\n".join(
                str(t.path)
                for t in removal.removable_trees(self.config, profile)
                if t.key in chosen
            )
            answer = QMessageBox.warning(
                self,
                "Delete game files?",
                "This permanently deletes these folders and everything in them:\n\n"
                f"{paths_text}\n\nThis cannot be undone.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._remove_game(profile, delete_data.isChecked(), chosen)

    def _remove_game(
        self, profile: str, delete_data: bool, delete_trees: list[str] | None = None
    ) -> None:
        """Run the slow cleanup off the GUI thread, then drop the profile here."""
        from ..core import removal

        # The worker deletes files but cannot persist the profile change, so make
        # sure that final save can succeed before anything irreversible happens.
        try:
            with self.config.locked():
                self.config.save()
        except OSError as exc:
            title = self._game_title(profile)
            self._set_side_status(f"Could not remove {title}: {exc}")
            QMessageBox.critical(self, "Remove game", f"Could not save config: {exc}")
            return

        self._busy[profile] = "remove"
        self._set_side_status(f"Removing {self._game_title(profile)}…")

        def run() -> object:
            return removal.remove_game(
                self.config,
                profile,
                delete_data=delete_data,
                delete_trees=delete_trees or (),
                drop_profile=False,
                save=False,
                desktop_dir=shortcuts.desktop_dir(),
            )

        task = CallTask(run)
        task.signals.done.connect(lambda result: self._on_game_removed(profile, result))
        task.signals.failed.connect(lambda message: self._on_remove_failed(profile, message))
        QThreadPool.globalInstance().start(task)

    def _on_game_removed(self, profile: str, result: object) -> None:
        title = self._game_title(profile)
        self._busy.pop(profile, None)
        self._snapshots.pop(profile, None)
        self._status_errors.pop(profile, None)
        self._remember_notice.pop(profile, None)
        self._remember_watching.discard(profile)
        self._remember_pending.discard(profile)
        save_error = ""
        if not getattr(result, "profile_kept", False):
            before = dict(self.config.profiles)
            before_active = self.config.data.get("active_profile", "")
            try:
                with self.config.locked():
                    self.config.remove_profile(profile)
                    self.config.save()
            except OSError as exc:
                # Not persisted: put the profile back so memory matches disk.
                self.config.profiles.clear()
                self.config.profiles.update(before)
                self.config.data["active_profile"] = before_active
                save_error = str(exc)
                QMessageBox.critical(self, "Remove game", f"Could not save config: {exc}")
        warnings = list(getattr(result, "warnings", []) or [])
        self.refresh_library()
        self.show_library()
        kept = getattr(result, "profile_kept", False)
        if save_error:
            self._set_side_status(f"Could not remove {title}: {save_error}")
        else:
            self._set_side_status(
                f"{title} was kept (some files could not be deleted)"
                if kept
                else f"Removed {title}"
            )
        if warnings:
            QMessageBox.warning(self, "Remove game", "\n".join(warnings))

    def _on_remove_failed(self, profile: str, message: str) -> None:
        self._busy.pop(profile, None)
        title = self._game_title(profile)
        self._set_side_status(f"Could not remove {title}")
        self.refresh_library()
        QMessageBox.critical(self, "Remove game", f"Could not remove {title}: {message}")

    def _on_settings_saved(self, profile: str) -> None:
        self._sync_remember(profile)
        self.refresh_library()
        self.show_library()

    def _on_remember_toggled(self, enabled: bool) -> None:
        """Remember me ticked on the game page or the login QR card."""
        profile = self._current_profile
        data = self.config.profiles.get(profile)
        if not data or not supports_remember_me(str(data.get("backend") or "")):
            return
        before = bool(data.get("remember_me", False))
        try:
            with self.config.locked():
                data["remember_me"] = bool(enabled)
                self.config.save()
        except OSError as exc:
            with self.config.locked():
                data["remember_me"] = before
            self.game.set_remember(True, before)
            QMessageBox.critical(self, "Remember me", f"Could not save config: {exc}")
            return
        self._sync_remember(profile)

    def _sync_remember(self, profile: str) -> None:
        """Apply the Remember me flag immediately (turning it off forgets the login)."""
        data = self.config.profiles.get(profile)
        if not data or not supports_remember_me(str(data.get("backend") or "")):
            return
        enabled = bool(data.get("remember_me", False))
        if profile in self._busy:
            # A worker owns the profile, so set_remember cannot start. Turning
            # Remember me off must still wipe the keyring, but the keyring/DBus
            # call can block, so it runs off the GUI thread.
            if not enabled and core_backends.supports_remember_me(str(data.get("backend") or "")):
                self._remember_pending.discard(profile)
                self._forget_saved_login(profile, str(data.get("backend") or ""))
            elif enabled:
                # Queue the enable until the worker frees the profile; dropping
                # it would leave the config asking for remember-me while no
                # credential is ever stored.
                self._remember_pending.add(profile)
            else:
                self._remember_pending.discard(profile)
            return
        if self._start(profile, "set_remember", enabled):
            # Watch the worker's status events so a "no keyring" notice still
            # reaches the user after the settings page has been left.
            self._remember_watching.add(profile)

    def _forget_saved_login(self, profile: str, kind: str) -> None:
        """Wipe the saved login off the GUI thread (keyring/DBus blocks)."""

        def run() -> None:
            core_backends.get(kind).forget_saved_login(profile)

        task = CallTask(run)
        task.signals.failed.connect(lambda message: self._on_forget_failed(profile, message))
        QThreadPool.globalInstance().start(task)

    def _on_forget_failed(self, profile: str, message: str) -> None:
        """Surface a failed credential wipe instead of silently swallowing it."""
        log.warning("could not remove the saved login for %s: %s", profile, message)
        if profile == self._current_profile:
            self._set_side_status("Could not remove the saved login")

    def _add_game_dialog(self, *, kind: str | None = None, advanced: bool = False) -> None:
        dialog = AddGameDialog(self.config, self, kind=kind, advanced=advanced)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.created_profile
        if not profile or profile not in self.config.profiles:
            return
        self.refresh_library()
        self.open_game(profile)
        if dialog.created_by_import:
            self._maybe_offer_verify(profile)  # a fresh install has nothing to check yet

    def _maybe_offer_verify(self, profile: str) -> None:
        """A Yostar import with a manifest can verify right away."""
        data = self.config.profiles.get(profile)
        if not data or str(data.get("backend") or "") not in (
            "arknights-yostar",
            "stellasora-yostar",
        ):
            return
        from ..core import yostar

        if not yostar.configured_manifest(data):
            return
        answer = QMessageBox.question(
            self,
            "Check game files",
            f"Check {self._game_title(profile)}'s game files against the official list now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start(profile, "verify")

    # ---- backend control ---------------------------------------------------
    def _start(
        self, profile: str, operation: str, payload: object = None, *, silent: bool = False
    ) -> bool:
        """Start ``operation``; ``silent`` marks a background refresh."""
        # Another process may have removed profiles since the last check (#376).
        self._sync_profiles_from_disk()
        if profile not in self.config.profiles:
            return False
        if operation in ("install", "update", "launch"):
            # What is installed before, so a runner downloaded by this operation is noticed.
            self._runners_before = {str(p) for p in core_runners.nadare_runners()}
        if not self.controller.start(profile, operation, payload):
            if not silent:
                QMessageBox.information(
                    self, "Busy", f"{profile} is already running an operation."
                )
            return False
        self._worker_generations[profile] = self._profile_generation(profile)
        return True

    def _snapshot(self, profile: str) -> dict[str, Any]:
        return self._snapshots.setdefault(profile, {})

    @staticmethod
    def _stale(stamp: Any, now: float) -> bool:
        return stamp is None or (now - float(stamp)) > SNAPSHOT_TTL

    def _after_status(self, profile: str) -> None:
        """Refresh news/media only for the game being shown, and only when stale."""
        if profile != self._current_profile:
            # Probing every profile at startup must not start a news storm; the
            # feed is fetched when the user actually opens that game.
            return
        now = time.monotonic()
        snap = self._snapshots.get(profile, {})
        # Direct-mode games publish a version manifest; refreshing it here keeps
        # status() offline and gives the UPDATE badge a cached value to compare.
        self._maybe_start_check_update(profile)
        if self._stale(snap.get("news_at"), now) and self._start_news(profile):
            return
        self._maybe_start_media(profile)

    def _maybe_start_check_update(self, profile: str) -> bool:
        """Ask for the published version at most once per profile every 6 h."""
        if not self.config.data.get("check_updates", True):
            return False
        if not self._has_direct_manifest(profile):
            return False
        now = time.monotonic()
        last = self._update_checked.get(profile)
        if last is not None and (now - last) < UPDATE_CHECK_INTERVAL:
            return False
        if not self._start(profile, "check_update", silent=True):
            return False
        self._update_checked[profile] = now
        return True

    def _has_direct_manifest(self, profile: str) -> bool:
        """Whether this profile's Yostar backend really uses a published manifest."""
        data = self.config.profiles.get(profile) or {}
        kind = str(data.get("backend") or "")
        if kind == "stellasora-yostar" or kind in DIRECT_KINDS:
            return True  # always direct-download
        if kind != "arknights-yostar":
            return False
        from ..core import yostar

        return bool(yostar.configured_manifest(data))

    def _maybe_start_media(self, profile: str) -> None:
        if profile != self._current_profile:
            return
        now = time.monotonic()
        snap = self._snapshots.get(profile, {})
        if self._stale(snap.get("media_at"), now):
            self._start_media(profile)

    def _on_game_action(self, operation: str) -> None:
        profile = self._current_profile
        if not profile:
            return
        if operation in NOTICE_OPERATIONS and self._needs_notice(profile):
            if not self._confirm_notice(profile):
                return
        if operation in UNOFFICIAL_OPERATIONS and not self._confirm_unofficial(profile):
            return
        if operation == "install" and not self._confirm_download(profile):
            return
        if operation == "repair" and not self._confirm_repair(profile):
            return
        self._start(profile, operation)

    def _on_remake_prefix_requested(self, profile: str) -> None:
        """Confirm from Settings, return to the game page and rebuild the prefix."""
        if profile not in self.config.profiles:
            return
        if not self._confirm_remake_prefix(profile):
            return
        self.open_game(profile)
        self._start(profile, "remake_prefix")

    def _confirm_remake_prefix(self, profile: str) -> bool:
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        answer = QMessageBox.question(
            self,
            "Rebuild Wine prefix",
            f"Rebuild the Wine prefix for {self._game_title(profile)}? Nadare moves "
            "the current one aside (it is kept), creates a fresh one and sets Windows "
            "components up again. Your game files are kept. Nothing is deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ---- unofficial-launcher notice and download size ------------------------
    def _backend_kind(self, profile: str) -> str:
        return str((self.config.profiles.get(profile) or {}).get("backend") or "")

    def _confirm_unofficial(self, profile: str) -> bool:
        """Tell the user once per game type that Nadare is unofficial; remember the answer."""
        kind = self._backend_kind(profile)
        if not core_backends.unofficial_notice(kind) or os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        acked = self.config.data.get("unofficial_notice_ack")
        if isinstance(acked, list) and kind in acked:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Before you continue")
        box.setText("Nadare is an unofficial launcher")
        box.setInformativeText(core_backends.UNOFFICIAL_NOTICE)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("I understand, continue")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        try:
            with self.config.locked():
                self.config.data["unofficial_notice_ack"] = sorted({*(acked or []), kind})
                self.config.save()
        except OSError as exc:
            QMessageBox.warning(
                self, "Nadare", f"Could not remember your answer ({exc}); you will be asked again."
            )
        return True

    def _confirm_download(self, profile: str) -> bool:
        """Ask before a fresh install that downloads several GB, showing the free space."""
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        size = core_backends.install_size_hint(self._backend_kind(profile))
        if not size or size < DOWNLOAD_CONFIRM_BYTES:
            return True
        status = self._snapshot(profile).get("status")
        if status is not None and getattr(status, "state", "") not in ("not_installed", ""):
            return True  # already installed: this is a repair, not a fresh download
        data = self.config.profiles.get(profile) or {}
        where = str(data.get("install_dir") or paths.profiles_dir())
        try:
            free = f"{diskspace.human_size(diskspace.free_bytes(where))} free"
        except OSError:
            free = "free space unknown"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Download the game?")
        box.setText(
            f"Installing {self._game_title(profile)} downloads about "
            f"{diskspace.human_size(size)} and needs about as much free space ({free})."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.button(QMessageBox.StandardButton.Yes).setText("Download")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _confirm_repair(self, profile: str) -> bool:
        """Ask once before a repair, which may re-download a lot of data."""
        if os.environ.get("NADARE_ASSUME_NOTICES"):
            return True
        answer = QMessageBox.question(
            self,
            "Repair files",
            f"Repair {self._game_title(profile)}? Nadare re-checks every game file "
            "and re-downloads the ones that are missing or damaged. "
            "This can download a lot of data.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ---- account notice ----------------------------------------------------
    def _notice_for(self, profile: str) -> str | None:
        """The backend's account notice, or ``None`` when it has none."""
        data = self.config.profiles.get(profile)
        if not data:
            return None
        try:
            backend = core_backends.get(str(data.get("backend") or ""))
        except core_backends.BackendError:
            return None
        notice = getattr(backend, "account_notice", None)
        return str(notice) if notice else None

    def _needs_notice(self, profile: str) -> bool:
        data = self.config.profiles.get(profile)
        if not data or bool(data.get("accept_account_notice", False)):
            return False
        return self._notice_for(profile) is not None

    def _confirm_notice(self, profile: str) -> bool:
        """Show the modal; persist the ack only when the user continues."""
        notice = self._notice_for(profile)
        if not notice:
            return True
        dialog = AccountNoticeDialog(notice, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        data = self.config.profiles[profile]
        try:
            with self.config.locked():
                data["accept_account_notice"] = True
                self.config.save()
        except OSError as exc:
            data["accept_account_notice"] = False
            QMessageBox.critical(self, "Account notice", f"Could not save config: {exc}")
            return False
        return True

    def _on_busy(self, profile: str, operation: str, active: bool) -> None:
        if operation in SILENT_OPERATIONS:
            # Background status/news/media refreshes never mark the game busy:
            # no CANCEL, no "CHECKING…" and no console expansion.
            return
        if active:
            self._busy[profile] = operation
            self._snapshot(profile).pop("events", None)  # a new run starts a new log
        else:
            self._busy.pop(profile, None)
            self._apply_pending_remember(profile)
            if (
                self._quit_after_games
                and not self.controller.launching_profiles()
                and not self.controller.mutating_profiles()
            ):
                self._quit_after_games = False
                QTimer.singleShot(0, self._finish_background_close)
        if profile == self._current_profile and active:
            self.game.set_busy(operation)
            label = _OPERATION_LABELS.get(operation, operation.replace("_", " ").capitalize())
            self._set_side_status(f"{label} · {self._game_title(profile)}")
            if operation in UNCANCELLABLE_OPERATIONS:
                # Verification cannot be stopped safely, so do not promise Cancel.
                self.game.primary.setText(f"{label.upper()}…")
                self.game.primary.setToolTip(
                    "This check cannot be cancelled. Wait for it to finish."
                )
                self.game.primary.setEnabled(False)

    def _on_cancel(self) -> None:
        if self._current_profile:
            self.controller.cancel(self._current_profile)

    # ---- controller fan-out -------------------------------------------------
    def _on_event(self, profile: str, event: Event) -> None:
        data = event.data or {}
        if event.kind == "step" and data.get("png"):
            self._snapshot(profile)["qr"] = data["png"]
        elif event.kind in ("done", "error") or data.get("qr_state") == "expired":
            # The QR is no longer live; do not resurrect it on a later revisit.
            self._snapshot(profile).pop("qr", None)
        if (
            event.kind == "status"
            and event.message
            and (
                profile in self._remember_watching
                or data.get("remember_unavailable")
                or str(event.message).startswith("Remember me unavailable")
            )
        ):
            self._remember_notice[profile] = event.message
        if profile in self._busy:
            # Kept so switching away and back replays the log and progress
            # instead of showing an empty console for a running install.
            self._snapshot(profile).setdefault("events", deque(maxlen=_REPLAY_EVENTS)).append(
                event
            )
        if profile == self._current_profile:
            self.game.append_event(event)

    def _on_result(self, profile: str, result: Any) -> None:
        if self._worker_generations.get(profile, self._profile_generation(profile)) != (
            self._profile_generation(profile)
        ):
            return  # the profile was removed/re-added after this worker started
        if isinstance(result, NewsResult):
            hero = getattr(result, "hero", None)
            if hero:
                self._record_official_art(profile, hero)
            snap = self._snapshot(profile)
            snap["news"] = result
            snap["news_at"] = time.monotonic()
            if profile == self._current_profile:
                self.game.set_news(result)
        elif isinstance(result, dict) and result.get("kind") == "media":
            hero = result.get("hero")
            if hero:
                self._record_official_art(profile, hero)
            snap = self._snapshot(profile)
            snap["media"] = result
            snap["media_at"] = time.monotonic()
            if profile == self._current_profile:
                self.game.set_media(result.get("hero"), result.get("banners") or [])
        elif isinstance(result, Status):
            snap = self._snapshot(profile)
            snap["status"] = result
            snap["status_at"] = time.monotonic()
            if result.state == "error":
                self._status_errors[profile] = self._status_errors.get(profile, 0) + 1
            else:
                self._status_errors.pop(profile, None)
            self.library.set_state(profile, result.state, result.running)
            self.sidebar_games.set_status(profile, result)
            if profile == self._current_profile:
                self.game.set_status(result)
                title = self._game_title(profile)
                if result.running or result.state == "running":
                    self._set_side_status(f"Playing {title}")
                else:
                    self._set_side_status(
                        f"{result.state.replace('_', ' ').title()} · {title}"
                    )
        elif isinstance(result, dict):
            msg = result.get("message")
            if result.get("verified", True):
                QMessageBox.information(self, "File check", str(msg or "All checks passed"))
            else:
                QMessageBox.warning(self, "File check failed", str(msg or "File check failed"))

    def _record_official_art(self, profile: str, path: str) -> None:
        """Persist publisher-art provenance on the GUI thread.

        Workers never touch the live ``config.data`` (that would race a
        concurrent ``Config.save()``); they return the hero path instead and the
        window records it here.
        """
        data = self.config.profiles.get(profile)
        if data is None:
            return
        from ..core import artwork

        with self.config.locked():
            if artwork.record_official(data, path, "hero"):
                try:
                    self.config.save()
                except OSError:
                    log.warning("could not save official-art provenance", exc_info=True)

    def _on_succeeded(self, profile: str, operation: str, exit_code: int) -> None:
        if self._closing:
            return
        if operation == "news":
            # One operation per profile: media runs after news, never alongside.
            self._maybe_start_media(profile)
            return
        if operation == "media":
            return
        if operation == "check_update":
            # The cached published version may now differ: refresh the badge.
            self._start(profile, "status", silent=True)
            return
        if operation not in SILENT_OPERATIONS:
            self._busy.pop(profile, None)
        if profile == self._current_profile:
            self.game.finish(operation, exit_code)
        if operation == "status":
            self._after_status(profile)
        else:
            self._start(profile, "status", silent=True)
        if operation == "set_remember" or profile in self._remember_notice:
            # The login/launch path can discover that Remember me cannot save
            # too; do not leave the box ticked with no explanation.
            self._show_remember_notice(profile)
        if operation in ("install", "update", "launch"):
            self._maybe_offer_runner_cleanup()

    # ---- old runners ----------------------------------------------------------
    def _maybe_offer_runner_cleanup(self) -> None:
        """After an operation that downloaded a runner, offer to delete unused old ones."""
        before = self._runners_before
        if before is None:
            return
        self._runners_before = None
        now = {str(p) for p in core_runners.nadare_runners()}
        if not (now - before):
            return  # nothing new was downloaded
        profiles = dict(self.config.profiles)
        # Usage matching and size measurement must not freeze the window.
        threading.Thread(target=self._measure_stale, args=(profiles,), daemon=True).start()

    def _measure_stale(self, profiles: dict) -> None:
        stale = core_runners.unused_old_runners(profiles)
        if not stale:
            return
        # Re-check in the same background pass: a game may have started using
        # one of them while the sizes were being measured.
        in_use = core_runners.runner_usage(profiles)
        pairs = [
            (path, core_runners.runner_size(path))
            for path in stale
            if not in_use.get(str(path))
        ]
        if not pairs:
            return
        try:
            self.runner_cleanup_ready.emit(pairs)
        except RuntimeError:
            pass  # the window closed meanwhile

    def _offer_runner_cleanup(self, items: object) -> None:
        pairs = list(items) if isinstance(items, list) else []
        if not pairs:
            return
        from .runners_dialog import human_size

        total = sum(size for _path, size in pairs)
        names = "\n".join(f"  • {p.name} ({human_size(size)})" for p, size in pairs)
        answer = QMessageBox.question(
            self,
            "Delete unused runners?",
            f"These older Proton runners are not used by any game ({human_size(total)}):"
            f"\n\n{names}\n\nDelete them to free the space? The recommended build is kept."
            "\nYou can also do this later under Settings → Manage runners.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # a destructive action never defaults to Yes
        )
        if answer == QMessageBox.StandardButton.Yes:
            threading.Thread(
                target=self._delete_stale, args=([p for p, _s in pairs],), daemon=True
            ).start()

    def _delete_stale(self, paths_in: list) -> None:
        deleted = failed = freed = 0
        for path in paths_in:
            try:
                size = core_runners.runner_size(path)
                ok = core_runners.remove(path)
            except OSError:
                ok = False
            if ok:
                deleted += 1
                freed += size
            else:
                failed += 1
        try:
            self.runner_cleanup_done.emit(deleted, freed, failed)
        except RuntimeError:
            pass

    def _runner_cleanup_finished(self, deleted: int, freed: int, failed: int) -> None:
        from .runners_dialog import human_size

        text = f"Deleted {count_text(deleted, 'unused runner')}, freed {human_size(freed)}"
        if failed:
            text += f" ({failed} could not be deleted)"
        self._set_side_status(text)

    def _show_remember_notice(self, profile: str) -> None:
        """Surface a ``set_remember`` status notice (e.g. no keyring) to the user."""
        self._remember_watching.discard(profile)
        notice = self._remember_notice.pop(profile, "")
        if notice:
            data = self.config.profiles.get(profile)
            usable = secrets.SecretStore(allow_fallback=False).secure
            if data and data.get("remember_me") and not usable:
                # Nothing can be saved, so a ticked box would only mislead.
                self._undo_remember(profile)
                notice += (
                    "\n\nRemember me has been turned off; "
                    "you will need to scan the QR code each time."
                )
            QMessageBox.information(self, "Remember me", notice)

    def _undo_remember(self, profile: str) -> None:
        """Clear the Remember me flag (and the boxes) when it cannot work."""
        data = self.config.profiles.get(profile)
        if not data:
            return
        try:
            with self.config.locked():
                data["remember_me"] = False
                self.config.save()
        except OSError:
            log.debug("could not save Remember me off", exc_info=True)
        if profile == self._current_profile:
            self.game.set_remember(True, False)

    def _apply_pending_remember(self, profile: str) -> None:
        """Apply a Remember-me change that was requested while the profile was busy."""
        if profile not in self._remember_pending:
            return
        self._remember_pending.discard(profile)
        self._sync_remember(profile)

    def _start_news(self, profile: str) -> bool:
        """Kick off the news fetch for profiles whose backend has a source."""
        data = self.config.profiles.get(profile)
        if not data or not supports_news(str(data.get("backend") or "")):
            return False
        return self._start(profile, "news", silent=True)

    def _start_media(self, profile: str) -> bool:
        """Kick off the official media fetch for capable backends."""
        data = self.config.profiles.get(profile)
        if not data or not supports_media(str(data.get("backend") or "")):
            return False
        return self._start(profile, "media", silent=True)

    def _mark_status_error(self, profile: str, message: str) -> None:
        """Show a failed check (and keep it in the snapshot) until a retry works."""
        kind = self._backend_kind(profile)
        if not kind or not core_backends.is_available(kind):
            # A raw "unknown backend: ..." is no help to someone who has to fix
            # (or re-add) the game (#366).
            message = _unknown_backend_message(kind)
        status = Status(state="error", message=message)
        self._status_errors[profile] = self._status_errors.get(profile, 0) + 1
        snap = self._snapshot(profile)
        snap["status"] = status
        snap["status_at"] = time.monotonic()
        self.library.set_state(profile, "error", False)
        self.sidebar_games.set_status(profile, status)
        if profile == self._current_profile:
            self.game.set_status(status)
            self._set_side_status(f"Error · {self._game_title(profile)}")

    def _on_failed(self, profile: str, operation: str, message: str) -> None:
        if self._closing:
            return
        if operation in ("news", "media", "check_update"):
            # News/media/update checks are best-effort: never a dialog, just the
            # muted UI line.
            if operation == "news" and profile == self._current_profile:
                self.game.set_news(NewsResult(items=[], available=False))
            if operation == "news":
                self._maybe_start_media(profile)
            if operation == "check_update":
                # Keep the page's normal refresh going even when offline.
                self._start(profile, "status", silent=True)
            return
        if operation == "set_remember":
            self._remember_watching.discard(profile)
            self._remember_notice.pop(profile, None)
        if operation not in SILENT_OPERATIONS:
            self._busy.pop(profile, None)
        if operation == "status":
            # A status probe is background noise; show it as a badge, never a dialog.
            self._mark_status_error(profile, message)
            return
        if profile == self._current_profile:
            self.game.fail(message)
        else:
            # Not the page on screen: say which game this is about.
            operation_title = OPERATION_TITLES.get(operation, operation.capitalize())
            QMessageBox.warning(
                self, f"{self._game_title(profile)}: {operation_title}", message
            )
        if profile in self._remember_notice:
            self._show_remember_notice(profile)
        if operation in REPROBE_OPERATIONS:
            # The failure may mean the files changed underneath us; never leave
            # the last READY state (and an enabled LAUNCH) on the page.
            self._mark_status_error(profile, message)
            self._start(profile, "status", silent=True)

    def _on_cancelled(self, profile: str, operation: str) -> None:
        if self._closing:
            return
        if operation not in SILENT_OPERATIONS:
            self._busy.pop(profile, None)
        if operation == "set_remember":
            self._remember_watching.discard(profile)
            self._remember_notice.pop(profile, None)
        if operation in SILENT_OPERATIONS or profile != self._current_profile:
            return
        self.game.cancelled()

    # ---- probing ------------------------------------------------------------
    def _probe_all(self) -> None:
        profiles = [profile for profile in self.config.profiles if profile != self._current_profile]
        if not profiles:
            return
        from ..core.process import ProcessScanRound

        scan_round = ProcessScanRound()
        for profile in profiles:
            self._start(profile, "status", payload=scan_round, silent=True)

    # ---- shutdown -----------------------------------------------------------
    # ---- update check ---------------------------------------------------------
    def _start_update_check(self) -> None:
        """Ask the release page once per start, off the GUI thread."""
        if not self.config.data.get("check_updates", True):
            return
        from ..core import updatecheck

        if updatecheck.managed_updates():
            # The AUR/Flatpak package manager owns updates; a release-page
            # button would tempt the user into a second, mixed install.
            return

        def run() -> None:
            try:
                info = updatecheck.check_latest()
                if info is None:
                    return
                self.update_found.emit(info)
            except RuntimeError:
                pass  # the window closed before the check finished
            except Exception:  # noqa: BLE001 - a background check must never die
                log.debug("update check failed", exc_info=True)

        threading.Thread(target=run, name="nadare-update-check", daemon=True).start()

    def _start_notices_check(self) -> None:
        """Read the project's notices feed once per start, off the GUI thread."""
        if not self.config.data.get("check_updates", True):
            return
        from ..core import notices

        def run() -> None:
            try:
                self.notices_found.emit(notices.load())
            except RuntimeError:
                pass  # the window closed before the feed arrived
            except Exception:  # noqa: BLE001 - a background check must never die
                log.debug("notices check failed", exc_info=True)

        threading.Thread(target=run, name="nadare-notices", daemon=True).start()

    def _show_notice(self, items: Any) -> None:
        from ..core import notices

        kinds = {str(p.get("backend") or "") for p in self.config.profiles.values()}
        dismissed = self.config.data.get("dismissed_notices") or []
        self._notice = notices.pick(items, __version__, kinds, dismissed=dismissed)
        if self._notice is None:
            self.notice_banner.hide_banner()
            return
        self.notice_banner.show_message(
            self._notice.text,
            "Learn more" if self._notice.link else "",
            dismissible=self._notice.severity != "block",
        )

    def _open_notice_link(self) -> None:
        link = self._notice.link if self._notice is not None else None
        if link and link.startswith("https://"):
            QDesktopServices.openUrl(QUrl(link))

    def _dismiss_notice(self) -> None:
        if self._notice is None:
            return
        self.notice_banner.hide_banner()
        seen = [str(x) for x in (self.config.data.get("dismissed_notices") or [])]
        self.config.data["dismissed_notices"] = [*seen, self._notice.id][-100:]
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the dismissed notice", exc_info=True)

    def _show_update(self, info: Any) -> None:
        self._update_url = str(info.url)
        self.update_button.setText(f"Update available: v{info.version}")
        self.update_button.setToolTip(f"{info.name}\n{info.url}")
        self.update_button.setVisible(True)
        self._update_version = str(info.version)
        if self.config.data.get("dismissed_update") != self._update_version:
            self.update_banner.show_message(
                f"Nadare v{info.version} is available (you have v{__version__}).",
                "Download",
            )

    def _dismiss_update(self) -> None:
        """Hide the banner for this release; the sidebar button stays."""
        self.update_banner.hide_banner()
        self.config.data["dismissed_update"] = self._update_version
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the dismissed update", exc_info=True)

    def _open_update_page(self) -> None:
        if self._update_url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(self._update_url))

    # ---- window geometry ------------------------------------------------------
    def _geometry_settings(self) -> QSettings:
        """Window size/position, kept beside the config (not inside it)."""
        path = self.config.path.with_name("window.ini")
        return QSettings(str(path), QSettings.Format.IniFormat)

    def _restore_geometry(self) -> None:
        """Reopen where the user left it; first run takes half the screen."""
        saved = self._geometry_settings().value("geometry")
        if isinstance(saved, QByteArray) and self.restoreGeometry(saved):
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1180, 780)
            return
        area = screen.availableGeometry()
        width = max(self.minimumWidth(), area.width() // 2)
        # leave room for the title bar and window border, which resize() excludes
        height = max(self.minimumHeight(), area.height() - 48)
        self.resize(min(width, area.width()), min(height, area.height()))
        self.move(area.topLeft())

    def _save_geometry(self) -> None:
        try:
            settings = self._geometry_settings()
            settings.setValue("geometry", self.saveGeometry())
            settings.sync()
        except Exception:  # noqa: BLE001 - never block closing on this
            pass

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Brought back (second start, activation): stay open after the game ends.
        self._quit_after_games = False
        self._close_requested_by_signal = False
        QApplication.setQuitOnLastWindowClosed(True)
        self._status_timer.start()
        super().showEvent(event)

    def _request_close_from_signal(self) -> None:
        """Close like the window button, but never wait on a question (SIGTERM/SIGINT)."""
        self._close_requested_by_signal = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._close_requested_by_signal and not self._confirm_close():
            event.ignore()
            return
        self._save_geometry()
        if self.controller.launching_profiles():
            # Never kill a running game by closing the launcher: drop the window
            # and everything else, and quit when the game exits.
            self.controller.shutdown(keep_launches=True)
            self._quit_after_games = True
            # A hidden last window would otherwise end the event loop (and the
            # game's worker with it).
            QApplication.setQuitOnLastWindowClosed(False)
            event.ignore()
            self.hide()
            return
        self._closing = True
        self.controller.shutdown()
        super().closeEvent(event)

    def _finish_background_close(self) -> None:
        """The last game exited while the window was hidden: really quit now."""
        if self.isVisible():
            return
        self.controller.shutdown()
        QApplication.quit()

    def _confirm_close(self) -> bool:
        """Warn before quitting while an install/update is still writing.

        The worker is cancelled cooperatively on shutdown, but if it misses the
        cancel window the process may be torn down mid-write; the user should
        choose that knowingly. A running game never needs a warning: closing
        the window keeps it running.
        """
        playing = set(self.controller.launching_profiles())
        active = [n for n in self.controller.mutating_profiles() if n not in playing]
        if not active:
            return True
        names = ", ".join(sorted(active))
        # Only an install/update/removal leaves half-written game files; a login
        # just gets cancelled.
        writes_files = any(
            self.controller.running_operation(n) in {"install", "update", "uninstall"}
            for n in active
        )
        if writes_files:
            title = "Operation in progress"
            text = (
                f"{names} is still installing or updating.\n\n"
                "Closing now can leave the game files incomplete. Close anyway?"
            )
        else:
            title = "Sign-in in progress"
            text = f"{names} is still signing in or changing its login.\n\nClose anyway?"
        answer = QMessageBox.warning(
            self,
            title,
            text,
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Close


__all__ = ["MainWindow", "PAGE_LIBRARY", "PAGE_GAME", "PAGE_SETTINGS", "PAGE_STORE"]
