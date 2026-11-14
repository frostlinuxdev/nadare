# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Offers made after a successful operation: unused runners, old quality packs, stale Steam
shortcuts.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import copy
import logging
import threading

from PyQt6.QtCore import (
    QThreadPool,
)
from PyQt6.QtWidgets import (
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    diskspace,
    shortcuts,
)
from ...core import runners as core_runners
from ...core.config import Config
from ..library import count_text
from ..workers import CallTask

log = logging.getLogger("nadare.gui.main_window")


class CleanupMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _start_steam_shortcut_cleanup(self) -> None:
        """Drop Steam shortcuts of games removed while Steam was running (#1031).

        Only when a Steam ``shortcuts.vdf`` exists (a few stats here) does a thread start; it
        changes nothing while Steam runs and nothing but Nadare's own orphaned entries.
        """
        # A recovered config or an empty one has no trustworthy list of profiles: every
        # shortcut would look orphaned and be deleted.
        if self.config.notice or not self.config.profiles:
            return
        try:
            if not shortcuts.find_steam_shortcut_files():
                return
        except OSError:
            return
        profiles = {name: dict(data) for name, data in self.config.profiles.items()}

        def run() -> None:
            try:
                result = shortcuts.refresh_steam_shortcuts(profiles, orphans_only=True)
            except Exception:  # noqa: BLE001 - a background tidy-up must never break start
                log.debug("Steam shortcut cleanup failed", exc_info=True)
                return
            for item in result.get("removed", []):
                log.info("removed the Steam shortcut of a removed game: %s", item)

        threading.Thread(target=run, name="steam-shortcut-cleanup", daemon=True).start()

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
        from ..runners_dialog import human_size

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
        from ..runners_dialog import human_size

        text = f"Deleted {count_text(deleted, 'unused runner')}, freed {human_size(freed)}"
        if failed:
            text += f" ({failed} could not be deleted)"
        self._set_side_status(text)

    # ---- unused resource qualities --------------------------------------------
    def _maybe_offer_quality_cleanup(self, profile: str) -> None:
        """After an install/update, offer to delete the old WuWa resource tiers (#990)."""
        data = self.config.profiles.get(profile)
        if not data:
            return
        try:
            backend_class = core_backends.get(str(data.get("backend") or ""))
        except core_backends.BackendError:
            return
        if not hasattr(backend_class, "other_quality_folders"):
            return
        try:
            backend = core_backends.create(profile, self.config)
        except core_backends.BackendError:
            return

        def measure() -> None:
            try:
                items = backend.other_quality_folders()
            except (OSError, core_backends.BackendError):
                log.debug("could not measure the old resource qualities", exc_info=True)
                return
            if not items:
                return
            try:
                self.quality_cleanup_ready.emit(profile, items)
            except RuntimeError:
                pass  # the window closed meanwhile

        threading.Thread(target=measure, name="quality-cleanup", daemon=True).start()

    def _offer_quality_cleanup(self, profile: str, items: object) -> None:
        tiers = list(items) if isinstance(items, (list, tuple)) else []
        if not tiers or profile not in self.config.profiles:
            return
        title = self._game_title(profile)
        from ...core.sources import wuwa

        current = wuwa.quality_of(self.config.profiles[profile].get("wuwa_quality"))
        if not self._ask_remove_qualities(title, tiers, current):
            return
        # Snapshot the settings the dialog was shown for: a Settings save while the slow
        # deletion runs must not change which tier is deleted.
        with self.config.locked():
            worker_config = Config(
                path=self.config.path, data=copy.deepcopy(self.config.data)
            )
        try:
            backend = core_backends.create(profile, worker_config)
        except core_backends.BackendError as exc:
            self._quality_cleanup_failed(profile, str(exc))
            return
        only = [str(tier[0]) for tier in tiers]
        task = CallTask(lambda: backend.remove_other_qualities(only=only))
        task.signals.done.connect(
            lambda result: self._quality_cleanup_finished(profile, tiers, result)
        )
        task.signals.failed.connect(
            lambda message: self._quality_cleanup_failed(profile, message)
        )
        QThreadPool.globalInstance().start(task)

    def _quality_cleanup_finished(
        self, profile: str, tiers: list, result: object
    ) -> None:
        if profile not in self.config.profiles:
            return
        freed, warnings = result if isinstance(result, tuple) else (0, [])
        names = " and ".join(str(tier[0]).upper() for tier in tiers)
        if freed > 0:
            text = f"Removed the {names} files: {diskspace.human_size(freed)} freed"
        else:
            # Refused on the re-check (the quality changed meanwhile): say so (stress round 2).
            text = f"The {names} files were kept: they are in use again"
        if warnings:
            text += " • " + " • ".join(str(warning) for warning in warnings)
        self._set_side_status(text)
        if profile == self._current_profile:
            self.game.append_line(text, "done")
        self._start(profile, "status", silent=True)

    def _quality_cleanup_failed(self, profile: str, message: str) -> None:
        text = f"Could not remove the old resource-quality files: {message}"
        if profile in self.config.profiles:
            self._set_side_status(text)
        if profile == self._current_profile:
            self.game.append_line(text, "error")

    def _ask_remove_qualities(self, title: str, items: list, current: str) -> bool:
        """The one modal for removing unused resource tiers; No is the default."""
        names = " and ".join(str(item[0]).upper() for item in items)
        total = sum(int(item[2]) for item in items)
        answer = QMessageBox.question(
            self,
            "Remove old resource files?",
            f"{title} now uses {current.upper()}. The {names} files "
            f"({diskspace.human_size(total)}) are no longer needed. Remove them now? "
            "The game keeps working.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # a destructive action never defaults to Yes
        )
        return answer == QMessageBox.StandardButton.Yes
