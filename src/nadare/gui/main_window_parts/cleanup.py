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
from pathlib import Path

from PyQt6.QtWidgets import (
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    diskspace,
    flatpakreport,
    gamesandbox,
    installercache,
    leftovers,
    runnerindex,
    sandboxreport,
    shortcuts,
)
from ...core import runners as core_runners
from ...core.config import Config
from ...core.events import Cancel, Cancelled, Event
from ..library import count_text
from ..workers import QualityCleanup, QualityCleanupResult

log = logging.getLogger("nadare.gui.main_window")


def runner_pin_text(change: runnerindex.Change, profiles: dict) -> str:
    """The one-line notice for a moved pin, plus a count for a critical one."""
    text = f"Recommended runner updated to {change.tag}."
    offers = runnerindex.critical_offers(change, profiles)
    if offers:
        count = len(offers)
        noun = "game" if count == 1 else "games"
        text += (
            f" Critical fix: {count} {noun} on {change.previous_tag} can switch in Settings."
        )
    return text


class CleanupMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _offer_runtime_profile_update(self) -> None:
        from ...core import userns
        from ..userns_dialog import UsernsDialog

        if getattr(self, "_runtime_profile_offered", False):
            return
        if userns.runtime_profile_outdated():
            self._runtime_profile_offered = True
            UsernsDialog(userns.RUNTIME_MOVED_MESSAGE + "\n" + userns.hint(), self).exec()

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

        argv = shortcuts.launcher_argv()

        def run() -> None:
            try:
                # Also repairs a shortcut whose Nadare is gone (an old AppImage; #1876).
                result = shortcuts.refresh_steam_shortcuts(
                    profiles, argv=argv, orphans_only=True, repair_stale=True
                )
            except Exception:  # noqa: BLE001 - a background tidy-up must never break start
                log.debug("Steam shortcut cleanup failed", exc_info=True)
                return
            for item in result.get("removed", []):
                log.info("removed the Steam shortcut of a removed game: %s", item)

        threading.Thread(target=run, name="steam-shortcut-cleanup", daemon=True).start()

    def _start_shortcut_repair(self, *, wait: bool = False) -> None:
        """Point menu and desktop shortcuts whose Nadare is gone at this one (#1876).

        An AppImage replaced by a newer file, or a removed checkout, left every shortcut made
        from it doing nothing. Only entries whose program no longer exists change; it runs in a
        thread because finding the Desktop folder may start ``xdg-user-dir``.
        """
        if self.config.notice or not self.config.profiles:
            return
        profiles = {name: dict(data) for name, data in self.config.profiles.items()}
        # Resolved now, not in the thread: the environment can change before it runs.
        applications = shortcuts.applications_dir()
        home = Path.home()
        argv = shortcuts.launcher_argv()

        def run() -> None:
            try:
                desktop: Path | None = shortcuts.desktop_dir()
                if desktop is not None and not desktop.is_relative_to(home):
                    desktop = None  # only the player's own Desktop folder
                result = shortcuts.repair_stale_desktop_entries(
                    profiles, applications_dir=applications, desktop_dir=desktop, argv=argv
                )
            except Exception:  # noqa: BLE001 - a background tidy-up must never break start
                log.debug("shortcut repair failed", exc_info=True)
                return
            for path in result.get("updated", []):
                log.info("pointed the shortcut %s at this Nadare (its old one is gone)", path)
            for warning in result.get("warnings", []):
                log.info("%s", warning)

        thread = threading.Thread(target=run, name="shortcut-repair", daemon=True)
        thread.start()
        if wait:
            thread.join()

    # ---- crash leftovers and unneeded installers (#1417) ------------------------------
    def _start_leftover_sweep(self) -> None:
        """Remove what a killed run left behind and installers no library game needs (#1417).

        Leftovers: Nadare's own per-run names older than a day whose owner is gone
        (:mod:`nadare.core.leftovers`). Installers: only with a trustworthy list of games
        (:mod:`nadare.core.installercache`). Runs in a thread: it walks a few cache folders.
        """
        profiles = None if self.config.notice else copy.deepcopy(dict(self.config.profiles))
        threading.Thread(
            target=self._run_leftover_sweep, args=(profiles,), name="leftover-sweep", daemon=True
        ).start()

    def _run_leftover_sweep(self, profiles: dict | None) -> None:
        removed, freed = 0, 0
        for label, step in (
            ("leftover", leftovers.sweep),
            ("installer", None if profiles is None else lambda: installercache.sweep(profiles)),
        ):
            if step is None:
                continue
            try:
                result = step()
            except Exception:  # noqa: BLE001 - a start-up tidy-up must never break start-up
                log.debug("%s sweep failed", label, exc_info=True)
                continue
            removed += len(result.removed)
            freed += result.freed
        if removed:
            log.info(
                "removed %d leftover file(s) and unneeded installer(s) (%s)",
                removed,
                diskspace.human_size(freed),
            )

    # ---- Sandbox folders (#1299) ---------------------------------------------------
    def _start_sandbox_broad_check(self) -> None:
        """Tell the player once which games the Sandbox will refuse to start, and why (#1299).

        An older profile never chose the Sandbox (on by default since b22) and its folder may be
        one the launch refuses to bind; it used to learn that only at Play.
        """
        if self.config.notice or not self.config.profiles:
            return
        profiles = copy.deepcopy(dict(self.config.profiles))
        seen = list(self.config.data.get("sandbox_broad_seen") or [])
        threading.Thread(
            target=self._run_sandbox_broad_check,
            args=(profiles, seen),
            name="sandbox-folders",
            daemon=True,
        ).start()

    def _run_sandbox_broad_check(self, profiles: dict, seen: list) -> None:
        try:
            new = sandboxreport.unseen(sandboxreport.broad_folders(profiles), seen)
        except Exception:  # noqa: BLE001 - a start-up hint must never break start-up
            log.debug("Sandbox folder check failed", exc_info=True)
            return
        if not new:
            return
        try:
            self.sandbox_broad_found.emit(new)
        except RuntimeError:
            pass  # the window closed meanwhile

    def _show_sandbox_broad(self, items: object) -> None:
        found = list(items) if isinstance(items, list) else []
        if not found:
            return
        seen = list(self.config.data.get("sandbox_broad_seen") or [])
        seen.extend(item.key for item in found if item.key not in seen)
        self.config.data["sandbox_broad_seen"] = seen
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the Sandbox folder note", exc_info=True)
        QMessageBox.warning(self, "Sandbox will stop these games", sandboxreport.message(found))

    # ---- Flatpak folders (#1299) ---------------------------------------------------
    def _start_flatpak_reach_check(self) -> None:
        """Tell the player once which games sit in folders the Flatpak cannot see (#1299).

        An older Nadare (the AppImage, or a Flatpak with the whole host) may have put a game in
        ``/srv`` or ``/data``; inside today's Flatpak it simply looks not installed.
        """
        if self.config.notice or not self.config.profiles:
            return
        profiles = copy.deepcopy(dict(self.config.profiles))
        seen = list(self.config.data.get("flatpak_reach_seen") or [])
        threading.Thread(
            target=self._run_flatpak_reach_check,
            args=(profiles, seen),
            name="flatpak-folders",
            daemon=True,
        ).start()

    def _run_flatpak_reach_check(self, profiles: dict, seen: list) -> None:
        try:
            new = flatpakreport.unseen(flatpakreport.out_of_reach(profiles), seen)
        except Exception:  # noqa: BLE001 - a start-up hint must never break start-up
            log.debug("Flatpak folder check failed", exc_info=True)
            return
        if not new:
            return
        try:
            self.flatpak_reach_found.emit(new)
        except RuntimeError:
            pass  # the window closed meanwhile

    def _show_flatpak_reach(self, items: object) -> None:
        found = list(items) if isinstance(items, list) else []
        if not found:
            return
        seen = list(self.config.data.get("flatpak_reach_seen") or [])
        seen.extend(item.key for item in found if item.key not in seen)
        self.config.data["flatpak_reach_seen"] = seen
        try:
            self.config.save()
        except OSError:
            log.debug("could not remember the Flatpak folder note", exc_info=True)
        QMessageBox.warning(
            self, "The Flatpak cannot see these games", flatpakreport.message(found)
        )

    # ---- Sandbox runtime trust (#1299) --------------------------------------------
    def _start_sandbox_trust_check(self) -> None:
        """Ask once per start whether to trust a runtime bubblewrap umu updated (#1299).

        Nadare runs only the ``srt-bwrap`` it recorded (#871). umu's runtime updates change it,
        and where the system's bubblewrap is blocked (Ubuntu 24.04+) the Sandbox then had no
        copy left and games played without it.
        """
        if self.config.notice or not self.config.profiles:
            return
        profiles = copy.deepcopy(dict(self.config.profiles))
        threading.Thread(
            target=self._run_sandbox_trust_check,
            args=(profiles,),
            name="sandbox-trust",
            daemon=True,
        ).start()

    def _run_sandbox_trust_check(self, profiles: dict) -> None:
        try:
            wanted = any(
                isinstance(profile, dict)
                and gamesandbox.wanted(
                    profile,
                    supported=core_backends.supports_sandbox(str(profile.get("backend") or "")),
                    default=core_backends.sandbox_default(str(profile.get("backend") or "")),
                    required=core_backends.sandbox_required(str(profile.get("backend") or "")),
                )
                for profile in profiles.values()
            )
            copies = gamesandbox.trust_question() if wanted else []
        except Exception:  # noqa: BLE001 - a start-up hint must never break start-up
            log.debug("Sandbox runtime check failed", exc_info=True)
            return
        if not copies:
            return
        try:
            self.sandbox_trust_found.emit(copies)
        except RuntimeError:
            pass  # the window closed meanwhile

    def _ask_sandbox_trust(self, items: object) -> None:
        copies = [str(item) for item in items] if isinstance(items, list) else []
        if not copies:
            return
        listed = "\n".join(f"  • {item}" for item in copies)
        answer = QMessageBox.question(
            self,
            "Trust the new copy?",
            "Sandbox runs games with the bubblewrap that comes with the game runtime, and "
            "Nadare only runs the copy it recorded. That copy changed, usually because the "
            "game runtime updated itself:\n\n"
            f"{listed}\n\n"
            "Until you trust it, games run without the Sandbox (or do not start, when you "
            "switched the Sandbox on). Trust the new copy? Choose No if you did not expect "
            "an update; Nadare asks again at the next start.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            gamesandbox.trust_runtime_copies(copies)
        except gamesandbox.RuntimeBusy:
            QMessageBox.information(
                self,
                "Trust the new copy?",
                "A game is running, so Nadare cannot check the copy now. Close the game and "
                "restart Nadare to be asked again.",
            )
            return
        except OSError as exc:
            QMessageBox.warning(self, "Trust the new copy?", f"Could not record it: {exc}")
            return
        self._sandbox_ready_answer = None  # probe again: the Sandbox can start now
        self._sync_sandbox(self._current_profile)

    # ---- runner prefetch (#1425) ---------------------------------------------
    def _start_runner_prefetch(self) -> None:
        """Fetch a bumped runner pin in the background, so Play never waits for it (#1425).

        Only with update checks on and a trustworthy list of games; when something was
        downloaded, the usual "Delete unused runners?" offer follows for the old pin.
        """
        if not self.config.data.get("check_updates", True):
            return
        if self.config.notice:
            return
        thread = getattr(self, "_runner_prefetch_thread", None)
        if thread is not None and thread.is_alive():
            return
        cancel = Cancel()
        self._runner_prefetch_cancel = cancel
        profiles = copy.deepcopy(dict(self.config.profiles))
        self._runner_prefetch_thread = threading.Thread(
            target=self._run_runner_prefetch,
            args=(profiles, cancel),
            name="runner-prefetch",
            daemon=True,
        )
        self._runner_prefetch_thread.start()

    def _run_runner_prefetch(self, profiles: dict, cancel: Cancel | None = None) -> None:
        token = cancel or self._runner_prefetch_cancel
        if token is None:
            return
        signal = getattr(self, "runner_download_event", None)

        def emit(event: Event) -> None:
            if signal is not None:
                try:
                    signal.emit(token, event)
                except RuntimeError:
                    token.cancel()  # the window was destroyed

        def announce(changes: list) -> None:
            if changes:
                try:
                    self.runner_pin_updated.emit(changes)
                except RuntimeError:
                    pass  # the window closed meanwhile

        try:
            got = core_runners.prefetch(profiles, emit=emit, cancel=token, on_change=announce)
        except Cancelled:
            return
        except Exception:  # noqa: BLE001 - a background fetch must never break start-up
            log.debug("runner prefetch failed", exc_info=True)
            return
        finally:
            emit(Event("runner_end"))
        if not got:
            return
        log.info("fetched the updated runner in the background: %s", ", ".join(p.name for p in got))
        self._measure_stale(profiles)

    def _announce_pin_change(self, changes: object) -> None:
        """The signal slot for a changed runner recommendation."""
        self._show_runner_pin_updated(changes)

    def _show_runner_pin_updated(self, changes: object) -> None:
        """Say once that a signed pin moved; a critical one only points at Settings.

        Nothing is changed here: a running game and a profile with its own runner stay
        put. Games on the previous recommended build get the existing per-game choice.
        """
        items = list(changes) if isinstance(changes, list) else []
        if not items:
            return
        profiles = dict(self.config.profiles)
        offers = [
            name for change in items for name in runnerindex.critical_offers(change, profiles)
        ]
        self._runner_update_profile = offers[0] if offers else ""
        text = "\n".join(runner_pin_text(change, profiles) for change in items)
        self.runner_banner.show_message(text, "Settings")

    def _review_runner_update(self) -> None:
        """Open the runner choice in Settings, without changing a profile."""
        profile = self._runner_update_profile
        self.runner_banner.hide_banner()
        if profile and profile in self.config.profiles:
            self.open_game(profile)
        self.open_settings()

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
            (path, core_runners.runner_size(path)) for path in stale if not in_use.get(str(path))
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
            "\nYou can also do this later in Settings, under Disk space.",
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
            worker_config = Config(path=self.config.path, data=copy.deepcopy(self.config.data))
        only = tuple(str(tier[0]) for tier in tiers)
        # A controller operation like an install (#1474): one per game, the cross-process
        # operation lock, busy on the page, and counted when quitting. A game that is busy
        # gets the usual Busy box; the cleanup is offered again after its next update.
        self._start(profile, "remove_qualities", QualityCleanup(only, worker_config))

    def _quality_cleanup_finished(self, profile: str, result: QualityCleanupResult) -> None:
        if profile not in self.config.profiles:
            return
        freed, warnings = result.freed, result.warnings
        names = " and ".join(tier.upper() for tier in result.only)
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
