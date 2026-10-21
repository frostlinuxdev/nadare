# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The window shell: a sidebar plus library, per-game and settings pages.

``MainWindow`` is the only object that knows about both the UI and the backend
controller. It never calls a backend method directly — every request goes through
:class:`~nadare.gui.workers.BackendController`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from PyQt6.QtCore import QByteArray, QSettings, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
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

from ..core import backends as core_backends
from ..core import paths, secrets, shortcuts
from ..core.backends import (
    Status,
    supports_login,
    supports_media,
    supports_news,
    supports_remember_me,
)
from ..core.config import Config
from ..core.events import Event
from ..core.news import NewsResult
from . import i18n
from .add_game import AddGameDialog
from .game_view import GameSnapshot, GameView
from .library import LibraryPage
from .notice import AccountNoticeDialog
from .settings import SettingsPage
from .sidebar import SidebarGames
from .theme import app_icon, apply_theme, brand_pixmap, repolish
from .workers import BackendController, CallTask

PAGE_LIBRARY = 0
PAGE_GAME = 1
PAGE_SETTINGS = 2

#: Operations that must not run until the account notice is acknowledged.
NOTICE_OPERATIONS = frozenset({"install", "update", "launch"})

#: A snapshot younger than this skips the news/media refetch on a switch.
#: ``status`` always refreshes, but silently.
SNAPSHOT_TTL = 5 * 60

#: Background refreshes that must never put the page into the busy state.
SILENT_OPERATIONS = frozenset({"status", "news", "media"})

log = logging.getLogger(__name__)


#: Side-status wording for operations whose name reads badly as-is.
_OPERATION_LABELS = {"set_remember": "Saving"}


class MainWindow(QMainWindow):
    """Nadare's main window."""

    #: A newer release was found by the background update check.
    update_found = pyqtSignal(object)

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
        #: Last rendered state per profile (status/news/media + monotonic stamp).
        self._snapshots: dict[str, dict[str, Any]] = {}
        #: Profiles whose pending ``set_remember`` notice must reach the user.
        self._remember_watching: set[str] = set()
        self._remember_notice: dict[str, str] = {}
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
        self.setMinimumSize(960, 640)
        self._restore_geometry()

        self._build_shell()

        self.controller = BackendController(config, self)
        self._wire_controller()
        self.update_found.connect(self._show_update)
        self._start_update_check()

        self.refresh_library()
        if profile and profile in self.config.profiles:
            self.open_game(profile)
        elif self.config.active in self.config.profiles:
            self.open_game(self.config.active)
        else:
            self.show_library()
        self._probe_all()

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
        self.stack.addWidget(self.library)
        self.stack.addWidget(self.game)
        self.stack.addWidget(self.settings)
        root.addWidget(self.stack, 1)

        # UI intent -> controller / navigation.
        self.library.open_game.connect(self.open_game)
        self.library.add_game.connect(self._add_game)
        self.game.back_requested.connect(self.show_library)
        self.game.action_requested.connect(self._on_game_action)
        self.game.cancel_requested.connect(self._on_cancel)
        self.game.remember_toggled.connect(self._on_remember_toggled)
        self.game.settings_requested.connect(self.open_settings)
        self.settings.saved.connect(self._on_settings_saved)
        self.settings.cancelled.connect(self.show_library)
        self.settings.remove_requested.connect(self._on_remove_requested)
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
        side.addSpacing(8)
        side.addWidget(self.update_button)
        side.addSpacing(26)

        nav_label = QLabel("LIBRARY")
        nav_label.setObjectName("Section")
        side.addWidget(nav_label)
        side.addSpacing(4)

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
        status_row.addWidget(self._status_dot)
        self.side_status = QLabel("No game selected")
        self.side_status.setObjectName("Dim")
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
        for name, button in (("library", self.library_button), ("settings", self.settings_button)):
            button.setProperty("active", "true" if name == active else "false")
            repolish(button)

    # ---- profiles / library ------------------------------------------------
    def refresh_library(self) -> None:
        """Rebuild the library cards and the sidebar game list from the config."""
        self.library.refresh(self.config.profiles)
        for name, snap in self._snapshots.items():
            st = snap.get("status")
            if st is not None and name in self.config.profiles:
                self.library.set_state(name, st.state, st.running)
        self.sidebar_games.set_profiles(self.config.profiles)

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
        self.side_status.setText("Library")

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
        self.side_status.setText(f"Playing {profile}")
        if profile in self._busy:
            self.game.set_busy(self._busy[profile])
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
        self.side_status.setText(f"Settings · {profile}")

    def _on_remove_requested(self, profile: str) -> None:
        """Confirm, then remove ``profile`` from the library (files kept by default)."""
        if profile not in self.config.profiles:
            return
        if profile in self._busy:
            QMessageBox.information(
                self, "Remove game", f"{profile} is busy. Stop it first, then remove it."
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
        delete_data = QCheckBox(f"Also delete Nadare's data for this game ({data_dir})")
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
                check.setEnabled(False)
                check.setToolTip(f"Not deleted: {tree.reason}")
                check.setText(f"{label} - kept: {tree.reason}")
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

        self._busy[profile] = "remove"
        self.side_status.setText(f"Removing {profile}…")

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
        self._busy.pop(profile, None)
        self._snapshots.pop(profile, None)
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
            self.side_status.setText(f"Could not remove {profile}: {save_error}")
        else:
            self.side_status.setText(
                f"{profile} was kept (some files could not be deleted)"
                if kept
                else f"Removed {profile}"
            )
        if warnings:
            QMessageBox.warning(self, "Remove game", "\n".join(warnings))

    def _on_remove_failed(self, profile: str, message: str) -> None:
        self._busy.pop(profile, None)
        QMessageBox.critical(self, "Remove game", f"Could not remove {profile}: {message}")

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
            self.side_status.setText("Could not remove the saved login")

    def _add_game(self) -> None:
        dialog = AddGameDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.created_profile
        if not profile or profile not in self.config.profiles:
            return
        self.refresh_library()
        self.open_game(profile)
        self._maybe_offer_verify(profile)

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
            "Verify files",
            f"Verify {profile}'s game files against the manifest now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start(profile, "verify")

    # ---- backend control ---------------------------------------------------
    def _start(
        self, profile: str, operation: str, payload: object = None, *, silent: bool = False
    ) -> bool:
        """Start ``operation``; ``silent`` marks a background refresh."""
        if not self.controller.start(profile, operation, payload):
            if not silent:
                QMessageBox.information(
                    self, "Busy", f"{profile} is already running an operation."
                )
            return False
        return True

    def _snapshot(self, profile: str) -> dict[str, Any]:
        return self._snapshots.setdefault(profile, {})

    @staticmethod
    def _stale(stamp: Any, now: float) -> bool:
        return stamp is None or (now - float(stamp)) > SNAPSHOT_TTL

    def _after_status(self, profile: str) -> None:
        """Refresh news/media only when the snapshot is older than the TTL."""
        now = time.monotonic()
        snap = self._snapshots.get(profile, {})
        if self._stale(snap.get("news_at"), now) and self._start_news(profile):
            return
        self._maybe_start_media(profile)

    def _maybe_start_media(self, profile: str) -> None:
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
        self._start(profile, operation)

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
            self.side_status.setText(f"{label} · {profile}")

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
            and profile in self._remember_watching
        ):
            self._remember_notice[profile] = event.message
        if profile == self._current_profile:
            self.game.append_event(event)

    def _on_result(self, profile: str, result: Any) -> None:
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
            self.library.set_state(profile, result.state, result.running)
            self.sidebar_games.set_status(profile, result)
            if profile == self._current_profile:
                self.game.set_status(result)
                self.side_status.setText(f"{result.state.replace('_', ' ').title()} · {profile}")
        elif isinstance(result, dict):
            msg = result.get("message")
            if result.get("verified", True):
                QMessageBox.information(self, "Verify", str(msg or "All checks passed"))
            else:
                QMessageBox.warning(self, "Verify failed", str(msg or "Verification failed"))

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
            artwork.record_official(data, path, "hero")

    def _on_succeeded(self, profile: str, operation: str, exit_code: int) -> None:
        if operation == "news":
            # One operation per profile: media runs after news, never alongside.
            self._maybe_start_media(profile)
            return
        if operation == "media":
            return
        if operation not in SILENT_OPERATIONS:
            self._busy.pop(profile, None)
        if profile == self._current_profile:
            self.game.finish(operation, exit_code)
        if operation == "status":
            self._after_status(profile)
        else:
            self._start(profile, "status", silent=True)
        if operation == "set_remember":
            self._show_remember_notice(profile)

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

    def _on_failed(self, profile: str, operation: str, message: str) -> None:
        if operation in ("news", "media"):
            # News/media are best-effort: never a dialog, just the muted UI line.
            if operation == "news" and profile == self._current_profile:
                self.game.set_news(NewsResult(items=[], available=False))
            if operation == "news":
                self._maybe_start_media(profile)
            return
        if operation == "set_remember":
            self._remember_watching.discard(profile)
            self._remember_notice.pop(profile, None)
        if operation not in SILENT_OPERATIONS:
            self._busy.pop(profile, None)
        if operation == "status":
            # A status probe is background noise; show it as a badge, never a dialog.
            self.library.set_state(profile, "error", False)
            self.sidebar_games.set_status(profile, Status(state="error", message=message))
            if profile == self._current_profile:
                self.game.set_status(Status(state="error", message=message))
                self.side_status.setText(f"Error · {profile}")
            return
        if profile == self._current_profile:
            self.game.fail(message)
        else:
            QMessageBox.warning(self, operation, message)

    def _on_cancelled(self, profile: str, operation: str) -> None:
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
        for profile in self.config.profiles:
            if profile == self._current_profile:
                continue
            self._start(profile, "status", silent=True)

    # ---- shutdown -----------------------------------------------------------
    # ---- update check ---------------------------------------------------------
    def _start_update_check(self) -> None:
        """Ask the release page once per start, off the GUI thread."""
        if not self.config.data.get("check_updates", True):
            return
        from ..core import updatecheck

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

    def _show_update(self, info: Any) -> None:
        self._update_url = str(info.url)
        self.update_button.setText(f"Update available: v{info.version}")
        self.update_button.setToolTip(f"{info.name}\n{info.url}")
        self.update_button.setVisible(True)

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
        height = max(self.minimumHeight(), area.height())
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
        QApplication.setQuitOnLastWindowClosed(True)
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._confirm_close():
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
        answer = QMessageBox.warning(
            self,
            "Operation in progress",
            f"{names} is still installing or updating.\n\n"
            "Closing now can leave the game files incomplete. Close anyway?",
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Close


__all__ = ["MainWindow", "PAGE_LIBRARY", "PAGE_GAME", "PAGE_SETTINGS"]
