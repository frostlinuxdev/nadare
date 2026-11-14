# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The window shell: a sidebar plus library, per-game and settings pages.

``MainWindow`` is the only object that knows about both the UI and the backend
controller. It never calls a backend method directly — every request goes through
:class:`~nadare.gui.workers.BackendController`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEvent,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)

# Names that tests and callers reach through this module; the code that uses them moved to
# main_window_parts/ (#1248). Re-exported so ``main_window.X`` is still the same object.
from PyQt6.QtGui import QDesktopServices as QDesktopServices  # noqa: E402
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtWidgets import QDialog as QDialog  # noqa: E402

from .. import __version__
from ..core import backends as core_backends  # noqa: E402, F401
from ..core import (
    channel,
    gamesandbox,
    selfupdate,
    steamstore,
)
from ..core import libraries as core_libraries
from ..core import runners as core_runners
from ..core import secrets as secrets  # noqa: E402
from ..core import shortcuts as shortcuts  # noqa: E402
from ..core.backends import Status as Status  # noqa: E402
from ..core.backends import (
    supports_login,
    supports_media,
    supports_news,
    supports_remember_me,
)
from ..core.config import Config
from ..core.directkinds import DIRECT_KINDS
from ..core.eta import EtaEstimator, format_eta
from ..core.events import Event
from . import i18n, notify, steam_setup, steam_wiring
from .banner import InfoBanner
from .downloads_page import DownloadRow, DownloadsPage, downloads_label
from .game_view import GameSnapshot, GameView
from .library import LibraryPage, profile_title
from .main_window_parts.accounts import (
    AccountsMixin,
)
from .main_window_parts.banners import (
    BannersMixin,
    graphics_banner_text,
)
from .main_window_parts.cleanup import (
    CleanupMixin,
)
from .main_window_parts.confirm import (
    NOTICE_OPERATIONS,
    UNOFFICIAL_OPERATIONS,
    ConfirmMixin,
    download_folder,
)
from .main_window_parts.found_copies import (
    FoundCopiesMixin,
)
from .main_window_parts.operations import (
    _OPERATION_LABELS,
    _REPLAY_EVENTS,
    REPROBE_OPERATIONS,
    SILENT_OPERATIONS,
    UNCANCELLABLE_OPERATIONS,
    OperationsMixin,
    _status_error_text,
    _unknown_backend_message,
)
from .main_window_parts.predownload import (
    PredownloadMixin,
)
from .main_window_parts.probes import (
    PROBE_ROUND_MAX_AGE,
    STARTUP_PROBE_LIMIT,
    STARTUP_PROBE_TIMEOUT,
    ProbesMixin,
)
from .main_window_parts.removal import (
    RemovalMixin,
    _removal_reason_text,
)
from .main_window_parts.selfupdate import (
    SelfUpdateMixin,
    capitalised,
)
from .main_window_parts.shared import (
    DOWNLOAD_CONFIRM_BYTES,
)
from .main_window_parts.store_actions import (
    StoreActionsMixin,
    _ask_check_files,
)
from .main_window_parts.window_state import (
    WindowStateMixin,
    _window_minimum,
    _window_size,
)
from .selfupdate_flow import SelfUpdateFlow
from .settings import SettingsPage
from .sidebar import SidebarGames
from .store_flow import StoreFlow
from .terms_dialog import TermsDialog
from .theme import app_icon, apply_theme, brand_pixmap, repolish
from .workers import PAUSABLE, BackendController

PAGE_LIBRARY = 0
PAGE_GAME = 1
PAGE_SETTINGS = 2
#: Appended last so the older page indexes never move.
PAGE_STORE = 3
PAGE_DOWNLOADS = 4

#: A snapshot younger than this skips the news/media refetch on a switch.
#: ``status`` always refreshes, but silently.
SNAPSHOT_TTL = 5 * 60
#: How long before a game page asks for the published version again (6 hours).
UPDATE_CHECK_INTERVAL = 6 * 60 * 60

#: How often a visible window re-probes the game page's status (#371).
STATUS_REFRESH_INTERVAL_MS = 30_000
#: Ceiling for the retry backoff after consecutive failed status probes of one game.
STATUS_ERROR_MAX_INTERVAL_MS = 10 * 60_000
log = logging.getLogger(__name__)


class MainWindow(
    AccountsMixin,
    BannersMixin,
    CleanupMixin,
    ConfirmMixin,
    FoundCopiesMixin,
    OperationsMixin,
    ProbesMixin,
    RemovalMixin,
    SelfUpdateMixin,
    StoreActionsMixin,
    WindowStateMixin,
    PredownloadMixin,
    QMainWindow,
):
    """Nadare's main window."""

    #: A newer release was found by the background update check.
    update_found = pyqtSignal(object)
    notices_found = pyqtSignal(object)
    #: Unused old runners measured in the background: a list of ``(path, bytes)``.
    runner_cleanup_ready = pyqtSignal(object)
    runner_cleanup_done = pyqtSignal(int, int, int)  # deleted, bytes freed, failed
    #: Unused WuWa resource tiers measured in the background: ``(profile, items)``.
    quality_cleanup_ready = pyqtSignal(str, object)
    #: Missing Flatpak packages were installed (or failed): ``(ok, message)``.
    graphics_installed = pyqtSignal(bool, str)
    #: The background Sandbox probe finished (#1207 b191).
    sandbox_ready_known = pyqtSignal(bool)
    #: Games found in another folder at start-up: ``{profile: [Copy, ...]}`` (#1293).
    found_copies_ready = pyqtSignal(object)

    def __init__(
        self, config: Config, profile: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._current_profile = ""
        self._graphics_fix = ""
        self._graphics_refs: tuple[str, ...] = ()
        self._installing_graphics = False
        self._settings_return = ""  # the game page Settings was opened from, if any
        self._busy: dict[str, str] = {}
        #: Profiles with a live predownload, kept apart from the main busy state: Play stays free.
        self._predownload_running: set[str] = set()
        # First installs the user stopped with "remove": the Remove dialog follows the stop.
        self._remove_after_cancel: set[str] = set()
        #: Profiles whose install/update/repair waits for a free install slot (#770).
        self._waiting: set[str] = set()
        self._steam_setup_dialog = None  # the one open Set up window, if any
        self._finish_timer = QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self._maybe_finish_background_close)
        #: Latest ``(fraction, detail, eta text)`` and the ETA estimator per install (#773).
        self._download_progress: dict[str, tuple[float | None, str, str]] = {}
        self._download_eta: dict[str, EtaEstimator] = {}
        #: Set when the window was closed while a game ran: Nadare stays alive,
        #: hidden, and quits once the last game exits.
        self._quit_after_games = False
        #: True once the window is really closing: callbacks must not start new work.
        self._closing = False
        self._terms_dialog: TermsDialog | None = None
        #: Terms offers that came in while a terms dialog was open, shown in turn (#799).
        self._terms_waiting: list[tuple[str, str]] = []
        #: A SIGTERM/SIGINT close skips the "still installing" question.
        self._close_requested_by_signal = False
        #: Last rendered state per profile (status/news/media + monotonic stamp).
        self._snapshots: dict[str, dict[str, Any]] = {}
        #: Consecutive failed status probes, used for per-profile retry backoff.
        self._status_errors: dict[str, int] = {}
        #: Failed file checks that stay on the page until a repair or a passing verify.
        self._verify_failures: dict[str, str] = {}
        #: Bumped whenever a profile disappears or reappears, so a result from a
        #: worker started for the old profile cannot land on its same-named
        #: replacement.
        self._generations: dict[str, int] = {}
        #: Generation the last foreground operation had when it started; direct
        #: callers without an operation token fall back to it.
        self._worker_generations: dict[str, int] = {}
        #: Generation each (profile, operation) had when its worker started, so
        #: a stale result cannot hide behind a newer operation of another kind.
        self._operation_generations: dict[tuple[str, str], int] = {}
        self._known_profiles: set[str] = set(config.profiles)
        #: Games found in another folder, and the ones the user said Not now to (#1293).
        self._found_copies: dict[str, list[Any]] = {}
        self._found_copies_ignored: set[str] = set()
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
        #: Startup status probes waiting for a free slot, and the ones running (#388 F-07).
        self._probe_queue: deque[str] = deque()
        self._probe_active: dict[str, float] = {}  # profile -> when its probe started
        self._probe_round: Any = None
        self._probe_round_at = 0.0

        # A hand-edited/imported language must never be used to build a path;
        # only codes with a real catalogue are accepted.
        language = str(config.data.get("language") or "en")
        if language not in i18n.available():
            language = i18n.DEFAULT_LANGUAGE
        i18n.set_language(language)
        apply_theme(self)
        self.setWindowTitle(channel.title(i18n.tr("Nadare")))
        self.setWindowIcon(app_icon())
        self.setMinimumSize(880, 560)  # measured: the game page needs 880 wide
        self._restore_geometry()

        self._build_shell()
        self._show_graphics_banner()

        self.store.install_requested.connect(lambda kind: self._store_install(kind))
        self.store.play_requested.connect(self._store_play)
        self.store.own_game_requested.connect(lambda: self._add_game_dialog(advanced=True))

        self.controller = BackendController(config, self)
        # A predownload may run for an hour; its own controller keeps the main slot free for Play.
        self.predownloads = BackendController(config, self)
        self._wire_controller()
        self.update_found.connect(self._show_update)
        self.notices_found.connect(self._show_notice)
        self.found_copies_ready.connect(self._show_found_copies)
        self.graphics_installed.connect(self._graphics_install_finished)
        self.sandbox_ready_known.connect(self._sandbox_probe_answered)
        self._sandbox_probe_running = False
        #: The background probe's answer, used when the cache cannot answer (never re-probed in a
        #: loop; a sandboxed launch refreshes the real cache anyway).
        self._sandbox_ready_answer: bool | None = None
        self.runner_cleanup_ready.connect(self._offer_runner_cleanup)
        self.runner_cleanup_done.connect(self._runner_cleanup_finished)
        self.quality_cleanup_ready.connect(self._offer_quality_cleanup)
        self._start_update_check()
        self._start_notices_check()
        self._start_steam_shortcut_cleanup()

        self._backfill_libraries()
        self.refresh_library()
        if profile and profile in self.config.profiles:
            self.open_game(profile)
        elif self.config.active in self.config.profiles:
            self.open_game(self.config.active)
        elif not self.config.profiles:
            self.show_store()  # nothing to show in the Library yet: start where games are
        else:
            self.show_library()
        self._probe_all()
        self._start_found_copies_check()

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
            # The same rule as removing a game here: no paused download or
            # running probe may survive the name for a later re-add.
            self.controller.forget(name)
            # Already gone from disk: do not let a later save treat it as ours to
            # delete, nor block another process from re-adding the same name.
            self.config._removed.discard(name)
            self.config._snapshot.pop(name, None)
            self._snapshots.pop(name, None)
            self._status_errors.pop(name, None)
            self._verify_failures.pop(name, None)
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
        self.downloads = DownloadsPage()
        self.stack.addWidget(self.library)
        self.stack.addWidget(self.game)
        self.stack.addWidget(self.settings)
        self.stack.addWidget(self.store)
        self.stack.addWidget(self.downloads)
        self.update_banner = InfoBanner()
        self.update_banner.action_clicked.connect(self._on_update_clicked)
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
        self.found_banner = InfoBanner()
        self.found_banner.action_clicked.connect(self._review_found_copies)
        self.found_banner.dismissed.connect(self.found_banner.hide_banner)
        right_column.addWidget(self.found_banner)
        self.graphics_banner = InfoBanner()
        self.graphics_banner.action_clicked.connect(self._on_graphics_action)
        self.graphics_banner.dismissed.connect(self.graphics_banner.hide_banner)
        right_column.addWidget(self.graphics_banner)
        right_column.addWidget(self.stack, 1)
        root.addWidget(right, 1)

        # UI intent -> controller / navigation.
        self.library.open_game.connect(self.open_game)
        self.library.play_game.connect(self._library_play)
        self.library.add_game.connect(self.show_store)
        self.game.back_requested.connect(self.show_library)
        self.game.action_requested.connect(self._on_game_action)
        self.game.found_copy_requested.connect(self._use_found_copy)
        self.game.cancel_requested.connect(self._on_cancel)
        self.game.skip_step_requested.connect(self._on_skip_step)
        self.game.pause_requested.connect(self._on_pause)
        self.game.resume_requested.connect(self._on_resume)
        self.game.queue_next_requested.connect(self._on_queue_next)
        self.game.queue_now_requested.connect(self._on_queue_now)
        self.game.remember_toggled.connect(self._on_remember_toggled)
        self.game.sandbox_toggled.connect(self._on_sandbox_toggled)
        self.game.settings_requested.connect(self.open_settings)
        saved_notice = self.config.data.get("page_notice_expanded")
        self.game.set_notice_preference(saved_notice if isinstance(saved_notice, bool) else None)
        self.game.notice_preference_changed.connect(self._save_notice_preference)
        self.game.set_console_preference(bool(self.config.data.get("output_expanded", False)))
        self.game.console_preference_changed.connect(self._save_console_preference)
        self.game.remove_requested.connect(self._on_remove_requested)
        self.game.move_requested.connect(self._open_move)
        self.library.remove_game.connect(self._on_remove_requested)
        self.game.report_requested.connect(self._show_support_report)
        self.game.steam_setup_requested.connect(self._open_steam_setup)
        self.settings.saved.connect(self._on_settings_saved)
        self.settings.notice.connect(lambda _profile, text: self._set_side_status(text))
        self.settings.download_requested.connect(self._on_quality_download_requested)
        self.settings.cancelled.connect(self._leave_settings)
        self.settings.remove_requested.connect(self._on_remove_requested)
        self.settings.remake_prefix_requested.connect(self._on_remake_prefix_requested)
        self.settings.report_requested.connect(self._show_support_report)
        self.settings.logs_requested.connect(self._show_logs)
        self.settings.system_check_requested.connect(self._show_system_check)
        self.settings.games_added.connect(lambda _names: self.refresh_library())
        self.settings.games_repaired.connect(self._on_games_repaired)
        self.sidebar_games.open_game.connect(lambda name: self._guarded(self.open_game, name))

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
        self.beta_pill.setToolTip(
            f"Nadare {__version__}, a beta: expect bugs. "
            "Use Report a problem on a game's page to tell us what went wrong."
        )
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
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("Hint")
        self.version_label.setToolTip("The Nadare version you are running.")
        side.addWidget(self.version_label)
        # Shown only when the startup check finds a newer release.
        self.update_button = QPushButton()
        self.update_button.setObjectName("GhostButton")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self._on_update_clicked)
        self._update_url = ""
        self._update_version = ""
        #: In-place AppImage update (#771): the release offered, the file installed and waiting
        #: for a restart, the progress dialog, and the worker that does the download.
        self._update_info: Any = None
        self._restart_target: Path | None = None
        self._update_progress: QProgressDialog | None = None
        self._self_update = SelfUpdateFlow(self)
        self._self_update.progress.connect(self._on_update_progress)
        self._self_update.installed.connect(self._on_update_installed)
        self._self_update.failed.connect(self._on_update_failed)
        self._self_update.cancelled.connect(self._on_update_cancelled)
        # The new version is running, so the copy of the old one kept for a bad update can go.
        with contextlib.suppress(Exception):
            running = selfupdate.appimage_target()
            if running is not None:
                selfupdate.cleanup_previous(running)
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
        self.store_button.clicked.connect(lambda: self._guarded(self.show_store))
        side.addWidget(self.store_button)

        self.library_button = QPushButton("Library")
        self.library_button.setObjectName("NavItem")
        self.library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_button.clicked.connect(lambda: self._guarded(self.show_library))
        side.addWidget(self.library_button)

        self.downloads_button = QPushButton("Downloads")
        self.downloads_button.setObjectName("NavItem")
        self.downloads_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.downloads_button.clicked.connect(lambda: self._guarded(self.show_downloads))
        side.addWidget(self.downloads_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("NavItem")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self._open_settings_from_sidebar)
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
        self.controller.paused.connect(self._on_paused)
        self.controller.busy.connect(self._on_busy)
        self.controller.queued.connect(self._on_queued)
        self.controller.queue_changed.connect(self._refresh_queue_view)
        self.controller.queue_changed.connect(self._refresh_downloads)
        self.controller.busy.connect(lambda *_args: self._refresh_downloads())
        self.controller.paused.connect(lambda *_args: self._refresh_downloads())
        # Cancelling a paused download emits only ``cancelled``: without this its row stayed.
        self.controller.cancelled.connect(lambda *_args: self._refresh_downloads())
        self.downloads.pause_requested.connect(lambda name: self.controller.pause(name))
        self.downloads.resume_requested.connect(lambda name: self.controller.resume(name))
        self.downloads.cancel_requested.connect(self._cancel_with_choice)
        self.downloads.next_requested.connect(lambda name: self.controller.move_to_front(name))
        self.downloads.now_requested.connect(
            lambda name: self.controller.start_now(name, self._can_pause_profile)
        )
        self.downloads.open_requested.connect(self.open_game)
        self.game.predownload_requested.connect(self._start_predownload)
        self.game.predownload_cancel_requested.connect(self._cancel_predownload)
        self.predownloads.busy.connect(self._on_predownload_busy)
        self.predownloads.backend_event.connect(self._on_predownload_event)
        self.predownloads.succeeded.connect(self._on_predownload_succeeded)
        self.predownloads.failed.connect(self._on_predownload_failed)
        self.predownloads.cancelled.connect(self._on_predownload_cancelled)

    # ---- navigation chrome -------------------------------------------------
    def _set_active_nav(self, active: str) -> None:
        buttons = (
            ("store", self.store_button),
            ("library", self.library_button),
            ("downloads", self.downloads_button),
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
                self.library.set_state(name, st)
        self.sidebar_games.set_hide_uninstalled(
            bool(self.config.data.get("sidebar_hide_uninstalled", False))
        )
        self.sidebar_games.set_profiles(self.config.profiles)
        self.store.set_installed_kinds(frozenset(steamstore.installed_kinds(self.config.profiles)))

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

    def show_downloads(self) -> None:
        self._refresh_downloads(force=True)
        self.stack.setCurrentIndex(PAGE_DOWNLOADS)
        self._current_profile = ""
        self._set_active_nav("downloads")
        self.sidebar_games.set_active("")
        self._set_side_status("Downloads")

    def _download_rows(self) -> list[DownloadRow]:
        overview = self.controller.install_overview()
        rows: list[DownloadRow] = []
        for name, operation in overview.running:
            if name not in self.config.profiles:
                continue
            fraction, detail, eta = self._download_progress.get(name, (None, "", ""))
            rows.append(
                DownloadRow(
                    name,
                    self._game_title(name),
                    operation,
                    "running",
                    detail=detail,
                    fraction=fraction,
                    eta=eta,
                    can_pause=operation in PAUSABLE and self._can_pause_profile(name),
                )
            )
        for index, (name, operation) in enumerate(overview.waiting):
            if name not in self.config.profiles:
                continue
            rows.append(
                DownloadRow(
                    name,
                    self._game_title(name),
                    operation,
                    "waiting",
                    ahead=index,
                    can_pause=operation in PAUSABLE and self._can_pause_profile(name),
                    can_now=self.controller.can_start_now(name, self._can_pause_profile),
                )
            )
        for name, operation in overview.paused:
            if name in self.config.profiles:
                rows.append(DownloadRow(name, self._game_title(name), operation, "paused"))
        return rows

    def _refresh_downloads(self, force: bool = False) -> None:
        """Keep the Downloads entry's count current, and the list when the page is open."""
        if self._closing:
            return
        overview = self.controller.install_overview()
        active = len(overview.running) + len(overview.waiting)
        text, tip = downloads_label(active, len(overview.paused))
        self.downloads_button.setText(text)
        self.downloads_button.setToolTip(tip)
        if force or self.stack.currentIndex() == PAGE_DOWNLOADS:
            self.downloads.set_rows(self._download_rows())

    def _track_download_progress(self, profile: str, event: Event) -> None:
        """Remember the latest progress of an install for the Downloads page."""
        if event.progress is None and event.kind not in ("step", "progress"):
            return
        fraction, detail, eta = self._download_progress.get(profile, (None, "", ""))
        if event.progress is not None:
            fraction = max(0.0, min(1.0, float(event.progress)))
            estimator = self._download_eta.setdefault(profile, EtaEstimator())
            eta = format_eta(estimator.update(fraction, time.monotonic())) or ""
        if event.kind in ("step", "progress") and event.message:
            detail = event.message
        self._download_progress[profile] = (fraction, detail, eta)
        if self.stack.currentIndex() == PAGE_DOWNLOADS:
            self.downloads.update_progress(profile, fraction, detail, eta)

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
        self.game.set_found_copy(self._found_copy_path(profile))
        self.game.set_predownload_progress(self.predownloads.is_busy(profile))
        self.game.set_account_controls(
            supports_login(str(self.config.profiles[profile].get("backend") or ""))
        )
        self.game.set_remember(
            supports_remember_me(str(self.config.profiles[profile].get("backend") or "")),
            bool(self.config.profiles[profile].get("remember_me", False)),
        )
        self._sync_sandbox(profile)
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
            if profile in self._waiting:
                self._show_waiting(profile, self._busy[profile])
            # A live login QR is a one-shot event; restore it from the snapshot
            # so leaving and returning does not lose the code for the rest of
            # the (up to 300 s) auth run.
            self.game.show_qr(snp.get("qr"))
        else:
            # Status always refreshes, but silently (no busy/CANCEL flash).
            self._start(profile, "status", silent=True)
            if self.controller.is_paused(profile):
                self.game.set_paused(self.controller.paused_operation(profile))

    def _guarded(self, action: Any, *args: Any) -> None:
        """Run a sidebar navigation, first asking when Settings holds unsaved edits (#852)."""
        if self.stack.currentIndex() == PAGE_SETTINGS and not self.settings.confirm_leave():
            return
        action(*args)

    def _open_settings_from_sidebar(self) -> None:
        """Clicking Settings while on Settings keeps the form as it is."""
        if self.stack.currentIndex() == PAGE_SETTINGS:
            return
        self.open_settings()

    def open_settings(self) -> None:
        profile = self._current_profile
        self._settings_return = profile
        if profile and profile in self.config.profiles:
            self.settings.load(self.config, profile)
            status = f"Settings · {self._game_title(profile)}"
        else:
            # No game open (Library, Store, Downloads, or no games at all): Nadare's own.
            self.settings.load_app_only(self.config)
            status = "Settings · Nadare"
        self.stack.setCurrentIndex(PAGE_SETTINGS)
        self._set_active_nav("settings")
        self.sidebar_games.set_active("")
        self._set_side_status(status)

    def _open_move(self, profile: str) -> None:
        """Open Settings on the tab where the game can be moved."""
        self.open_settings()
        self.settings.show_move_section()

    def _show_support_report(self, profile: str) -> None:
        from .support_report import ReportDialog, ReportTask

        if profile not in self.config.profiles:
            profile = self.config.active
        self._report_dialog = ReportDialog(self, profile)
        self._report_dialog.show()
        task = ReportTask(dict(self.config.data), profile)
        task.signals.finished.connect(self._report_dialog.set_report)
        QThreadPool.globalInstance().start(task)

    def _open_steam_setup(self) -> None:
        """Open the Set up checklist for the Steam game on the open page."""
        profile = self.config.profiles.get(self._current_profile) or {}
        app_id = steam_wiring.steam_app_id(profile)
        if not app_id:
            return
        existing = self._steam_setup_dialog
        if existing is not None:
            # One Set up window at a time: two could change the same files at once.
            try:
                if existing.isVisible():
                    if getattr(existing, "app_id", "") == app_id:
                        existing.raise_()
                        existing.activateWindow()
                        existing.refresh()  # the game may have been installed meanwhile
                        return
                    if steam_setup.steps_running():
                        self._tell_steam_problem(
                            "Another Set up step is still running. Wait for it to finish, "
                            "then open Set up for this game."
                        )
                        return
                    existing.close()  # an idle checklist for another game makes way
            except RuntimeError:  # already deleted
                pass
            self._steam_setup_dialog = None
        try:
            dialog = steam_wiring.build_dialog(
                self, app_id, binary=str(steam_wiring.profile_value(profile, "steam_binary"))
            )
        except steam_wiring.SteamUnavailable as exc:
            self._tell_steam_problem(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never leave the click without an answer
            log.debug("could not open Steam Set up", exc_info=True)
            self._tell_steam_problem(f"Set up could not start: {exc}")
            return
        self._steam_setup_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _ask_steam_step_close(self) -> bool:
        answer = QMessageBox.warning(
            self,
            "Steam Set up is working",
            "A Steam Set up step is still running.\n\nClosing now can leave Steam closed or a "
            "game's files half changed. Close anyway?",
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Close

    def _tell_steam_problem(self, text: str) -> None:
        QMessageBox.information(self, "Set up", text)

    def _show_system_check(self) -> None:
        from .system_check import SystemCheckDialog

        self._system_check_dialog = SystemCheckDialog(self, dict(self.config.data))
        self._system_check_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._system_check_dialog.show()
        self._system_check_dialog.run_check()

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

    @staticmethod
    def _removal_changed_nothing(result: object) -> bool:
        """True when Remove was refused before it deleted or forgot anything.

        Only removal itself knows (round 5: a tree that lost most of its files but still
        exists is not in ``trees_removed``), so this is its explicit ``refused`` mark.
        """
        return bool(getattr(result, "refused", False))

    def _restore_paused_page(self, profile: str) -> None:
        """After a Remove that changed nothing, show the paused download's RESUME again."""
        if profile == self._current_profile and self.controller.is_paused(profile):
            self.game.set_paused(self.controller.paused_operation(profile))

    def _on_settings_saved(self, profile: str) -> None:
        self._sync_remember(profile)
        self.refresh_library()
        self._leave_settings()

    def _on_quality_download_requested(self, profile: str) -> None:
        """Settings > "Download now" after a WuWa quality switch: the normal Update (#1335)."""
        if profile not in self.config.profiles:
            return
        if self.predownloads.is_busy(profile):
            self._show_predownload_error(
                profile, "Wait for the predownload to finish, or stop it first."
            )
            return
        if not self._confirm_action(profile, "update"):
            return
        self._start(profile, "update")

    def _leave_settings(self) -> None:
        """Go back to the game page Settings was opened from; the Library otherwise."""
        origin, self._settings_return = self._settings_return, ""
        if origin and origin in self.config.profiles:
            self.open_game(origin)
        else:
            self.show_library()

    def _sandbox_probe_answered(self, answer: bool) -> None:
        self._sandbox_ready_answer = answer
        self._sandbox_probe_running = False
        self._sync_sandbox(self._current_profile)

    def _probe_sandbox_in_background(self) -> None:
        if self._sandbox_probe_running:
            return
        self._sandbox_probe_running = True

        def run() -> None:
            try:
                answer = bool(gamesandbox.ready())
            except Exception:  # noqa: BLE001 - unknown means not ready
                answer = False
            try:
                self.sandbox_ready_known.emit(answer)
            except RuntimeError:
                self._sandbox_probe_running = False  # the window closed meanwhile

        threading.Thread(target=run, name="nadare-sandbox-probe", daemon=True).start()

    def _backfill_libraries(self) -> None:
        """Give games installed before libraries existed their manifest and library.

        Small and idempotent (one stat per game, a write only when something is missing or
        stale), so it runs before the first Library paint. Never fatal: the games work
        without it, and the next start tries again.
        """
        try:
            with self.config.locked():
                if core_libraries.backfill(self.config):
                    self.config.save()
        except Exception:  # noqa: BLE001 - start-up must not fail over a manifest
            log.warning("could not bring the game libraries up to date", exc_info=True)

    def _library_choices(self) -> list[tuple[str, str]]:
        """The install dialog's Library picker: (label, folder), the default library first."""
        default = core_libraries.default_root()
        return [
            ("Default library", "")
            if root == default
            else (core_libraries.display_path(root), str(root))
            for root in core_libraries.library_roots(self.config)
            # A library whose folder is gone (drive unplugged) is not offered: installing there
            # would make the folders on whatever disk is underneath. Settings still lists it.
            if root == default or root.is_dir()
        ]

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
                    self,
                    "Busy",
                    f"{self._game_title(profile)} is busy. Wait for it to finish, then try again.",
                )
            return False
        generation = self._profile_generation(profile)
        self._operation_generations[(profile, operation)] = generation
        if operation not in SILENT_OPERATIONS:
            self._worker_generations[profile] = generation
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
        if kind == "launcher-nikke":
            # Without its launcher Nadare keeps NIKKE's client current itself.
            extra = data.get("extra")
            nested = extra.get("launcherless") if isinstance(extra, dict) else None
            return data.get("launcherless", nested) is True
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

    def _download_only_reason(self, info: Any) -> tuple[str, str]:
        """Why the banner can only offer Download, and what to do about it (#1247)."""
        from ..core import selfupdate

        try:
            if selfupdate.asset_urls(info) is None:
                code = selfupdate.NO_ASSET
            else:
                code = selfupdate.why_not_replaceable()
            raw = os.environ.get("APPIMAGE")
            return selfupdate.download_only_reason(code, Path(raw) if raw else None)
        except Exception:  # noqa: BLE001 - only the explanation is lost, never the banner
            log.debug("could not work out why the update is download-only", exc_info=True)
            return "", ""

    def _set_update_tip(self, detail: str) -> None:
        self.update_banner.message_label.setToolTip(detail)
        self.update_banner.action_button.setToolTip(detail)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Brought back (second start, activation): stay open after the game ends.
        self._quit_after_games = False
        self._close_requested_by_signal = False
        if not self._closing:
            self.controller.reopen()
        QApplication.setQuitOnLastWindowClosed(True)
        self._status_timer.start()
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._close_requested_by_signal and not self._confirm_close():
            event.ignore()
            return
        self._save_geometry()
        if self.controller.launching_profiles():
            # Never kill a running game by closing the launcher: drop the window
            # and everything else, and quit when the game exits.
            self.controller.shutdown(keep_launches=True)
            self.predownloads.shutdown()
            self._quit_after_games = True
            # A hidden last window would otherwise end the event loop (and the
            # game's worker with it).
            QApplication.setQuitOnLastWindowClosed(False)
            event.ignore()
            self.hide()
            self._notify_still_running()
            return
        self._closing = True
        self.controller.shutdown()
        self.predownloads.shutdown()
        super().closeEvent(event)

    def _notify_still_running(self) -> None:
        """The window is gone but Nadare is not: say so once (#114, no tray icon)."""
        if self._close_requested_by_signal or not self.config.data.get("notify_when_done", True):
            return
        titles = [self._game_title(p) for p in self.controller.launching_profiles()]
        notify.send(*notify.still_running_text(titles))

#: The real Stop download question; tests stub the method (conftest) and call this one directly.
_REAL_ASK_CANCEL_CHOICE = MainWindow._ask_cancel_choice

__all__ = [
    "MainWindow",
    "PAGE_LIBRARY",
    "PAGE_GAME",
    "PAGE_SETTINGS",
    "PAGE_STORE",
    "PAGE_DOWNLOADS",
    "graphics_banner_text",
    "NOTICE_OPERATIONS",
    "UNOFFICIAL_OPERATIONS",
    "download_folder",
    "_REPLAY_EVENTS",
    "SILENT_OPERATIONS",
    "_unknown_backend_message",
    "_status_error_text",
    "UNCANCELLABLE_OPERATIONS",
    "REPROBE_OPERATIONS",
    "_OPERATION_LABELS",
    "STARTUP_PROBE_LIMIT",
    "PROBE_ROUND_MAX_AGE",
    "STARTUP_PROBE_TIMEOUT",
    "_removal_reason_text",
    "capitalised",
    "_ask_check_files",
    "_window_minimum",
    "_window_size",
    "DOWNLOAD_CONFIRM_BYTES",
]
