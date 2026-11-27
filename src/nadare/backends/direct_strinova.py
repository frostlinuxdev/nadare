# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Strinova, installed from the publisher's public archives."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..core import atomicfile, directdl, process, strinova_launcher
from ..core.backends import BackendError
from ..core.config import profile_value
from ..core.sources import strinova
from . import winegame
from .direct import GB, DirectBackend, game_subdir, log, publish_game_exports
from .webview2_signin import WebView2SignInMixin
from .wine import ProcessQuery


class StrinovaBackend(WebView2SignInMixin, DirectBackend):
    """Downloaded direct; started through the publisher launcher, which signs the player in.

    The launcher asks for WebView2 before it starts the game (AMD test desktop, 2026-11-27). As for
    Reverse: 1999 and GFL2 Haoplay, Nadare installs Microsoft's runtime itself rather than
    relying on the publisher's own offer.
    """

    game_process_names = ("Strinova-Win64-Shipping.exe",)
    kind = "direct-strinova"
    game_tag = "strinova"
    display_name = "Strinova"
    install_subdir = game_subdir("Strinova")
    direct_exe = strinova.EXE
    launcher_names = (direct_exe,)
    install_size_hint = int(61.5 * GB)
    plan_factory = staticmethod(strinova.resolve)
    #: The launcher's embedded browser and WebView2 draw blank under native Wayland; the
    #: hand test (#1819) used X11. ``display: wayland`` still wins.
    default_display = "x11"

    #: The publisher launcher and the patchers it runs while it updates the game in place.
    launcher_process_names = (
        strinova_launcher.LAUNCHER_EXE,
        "hpatchz.exe",
        "hpatch.exe",
        "UpdateGUI.exe",
        "startup_launcher.exe",
    )

    def launch(self) -> int:
        self._launching = True
        try:
            return super().launch()
        finally:
            self._launching = False
            # The launcher may have updated the game while it ran: follow it now, so the
            # page and the next Play agree with the files.
            self._adopt_after_launcher_exit()

    def _launcher_running(self) -> bool:
        return bool(
            winegame.scan_pids(
                ProcessQuery(
                    prefix=str(self.prefix()),
                    names=self.launcher_process_names,
                    require_name=True,
                )
            )
        )

    def _refuse_if_running(self, what: str) -> None:
        super()._refuse_if_running(what)
        # The launcher patches the same files in place; two writers would corrupt them.
        # Another Flatpak window's launcher is invisible to the scan, but holds the run lock.
        if self._launcher_running() or self._running_elsewhere():
            raise BackendError(f"Close the Strinova launcher before {what}", 8)

    def _adopt_after_launcher_exit(self) -> None:
        try:
            self._adopt_launcher_version()
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a launch
            log.debug("%s: could not read the launcher's version: %s", self.display_name, exc)

    def _adopt_launcher_version(self) -> str | None:
        """Record the version the publisher launcher updated the game to, if it did.

        Returns the version now recorded, or ``None`` when nothing changed. An unfinished
        Nadare install, a Nadare operation running now, or a launcher still open (perhaps
        still patching) is left alone.
        """
        newer = self._launcher_version_ahead()
        if newer is None:
            return None
        if self._launcher_busy():
            return None  # the next Play, or the launcher's exit, reads it again
        try:
            with directdl.install_lock(self.install_dir()):
                # Again under the lock: a Nadare update may have finished in between.
                if self._launcher_version_ahead() != newer or self._launcher_busy():
                    return None
                self._write_version_file(newer, self.game_tag)
                self._move_latest_forward(newer)
        except directdl.DirectError:
            return None  # another install or update holds the folder
        except OSError as exc:
            log.debug("%s: could not record the launcher's version: %s", self.display_name, exc)
            return None
        self.info(
            f"Strinova: the publisher launcher updated the game to {newer}; Nadare now "
            "records that version"
        )
        return newer

    def _launcher_busy(self) -> bool:
        """The launcher, a game started by another Flatpak window, or a Nadare operation."""
        return (
            process.operation_in_progress(self.ctx.name)
            or self._launcher_running()
            or self._running_elsewhere()
        )

    def _launcher_version_ahead(self) -> str | None:
        if self._install_incomplete():
            return None
        record = self._read_version_file() or {}
        return strinova_launcher.newer_installed_version(
            self.prefix(), self.install_dir(), record.get("version")
        )

    def _move_latest_forward(self, version: str) -> None:
        """The launcher found a newer version than the last check did: say so, or the page
        would offer the older one as an update. The check time is kept, so this never counts
        as a fresh server check; with no check cached there is nothing to correct."""
        latest = self._read_latest()
        published = strinova_launcher.version_key(latest["version"]) if latest else None
        wanted = strinova_launcher.version_key(version)
        if latest is None or published is None or wanted is None or published >= wanted:
            return
        atomicfile.write(
            self._latest_path(), json.dumps({**latest, "version": version}), make_parent=True
        )

    def _ensure_sign_in_runtime(self) -> None:
        # Only before a launch: install keeps its b29 behaviour (a failed runner download
        # still syncs the game files), and WebView2's installer needs the runner.
        if not getattr(self, "_launching", False):
            return
        # A library install outside the prefix may have none yet: wineboot (which cleans up
        # after itself on failure) goes first, not WebView2's installer.
        if not (self.prefix() / "drive_c").is_dir():
            self._ensure_prefix()
        super()._ensure_sign_in_runtime()

    def _ensure_game_drive(self) -> None:
        # The publisher launcher and its Game link live inside the prefix, so create it
        # before ensure_launcher writes there. Only launch() calls this hook: install()
        # must keep its b29 behaviour (a failed runner download still syncs the files,
        # and no wineboot runs when no runtime is available). Strinova has no game drive.
        if not (self.prefix() / "drive_c").is_dir():
            self._ensure_prefix()
        super()._ensure_game_drive()

    def _direct_argv(self, target: Path) -> list[str]:
        version = self._adopt_launcher_version() or (self._read_version_file() or {}).get(
            "version"
        )
        if version is None:
            self.info(
                "Strinova: this install has no recorded game version; "
                "the publisher launcher may offer to download the game again"
            )
        try:
            launcher = strinova_launcher.ensure_launcher(
                self.prefix(),
                self.install_dir(),
                version=version,
                data_dir=self._data_dir(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
            )
        except strinova_launcher.StrinovaLauncherError as exc:
            raise BackendError(f"cannot prepare the Strinova launcher: {exc}") from exc
        # The launcher starts the game itself and cannot forward Unreal arguments.
        if self._extra_args("launch_args") and not getattr(self, "_ignored_args_logged", False):
            self._ignored_args_logged = True
            self.info(
                "Strinova: extra launch arguments are ignored while the publisher launcher "
                "starts the game"
            )
        self.info("Strinova: starting the publisher launcher")
        return [str(launcher)]

    def plan_kwargs(self) -> dict[str, Any]:
        record = self._read_version_file() or {}
        return {"installed": record.get("version")}

    @staticmethod
    def _file_plan(plan: directdl.Plan) -> directdl.Plan:
        # Empty URLs are check-only metadata; this plan is never passed to sync.
        rows = plan.extra.get("verification_files")
        if rows is None:
            raise directdl.DirectError("the publisher's file list has no verification rows")
        return replace(
            plan,
            archives=(),
            files=tuple(
                directdl.RemoteFile(row["path"], "", row["size"], md5=row["md5"]) for row in rows
            ),
        )

    def _unsafe_files(self, plan: directdl.Plan) -> list[directdl.RemoteFile]:
        """Target files whose install path is a link, or a folder where the file belongs."""
        root = self.install_dir()
        unsafe = []
        for rf in self._file_plan(plan).files:
            try:
                directdl._safe_destination_path(root, rf.path, directory=False)
            except directdl.DirectError:
                unsafe.append(rf)
        return unsafe

    def _target_missing(self, plan: directdl.Plan, *, verify: bool = True) -> list[Any]:
        checks = self._file_plan(plan)
        root = self.install_dir()
        # plan_missing's loose-file check follows links; reject them first here.
        for rf in checks.files:
            directdl._safe_destination_path(root, rf.path, directory=False)
        return directdl.plan_missing(checks, root, verify=verify, cancel=self.ctx.cancel)[0]

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        plan = self.plan()
        root = self.install_dir()
        # A linked or folder-shadowed path is damage the user must fix, not an offline
        # manifest: report it, so verify() fails instead of saying "all checks passed".
        unsafe = self._unsafe_files(plan)
        checks = self._file_plan(plan)
        missing: list[Any] = list(unsafe)
        safe = tuple(rf for rf in checks.files if rf not in unsafe)
        try:
            if safe:
                missing.extend(
                    directdl.plan_missing(
                        replace(checks, files=safe), root, verify=verify, cancel=self.ctx.cancel
                    )[0]
                )
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        return missing

    @staticmethod
    def _inventory_plan(plan: directdl.Plan) -> directdl.Plan:
        # One stable receipt owns the complete tree, including files omitted by patches.
        inventory = directdl.Archive(
            "strinova-inventory",
            directdl.RemoteFile("strinova-inventory", strinova.BASE),
            slot="client",
        )
        return replace(plan, archives=(inventory,))

    def _before_sync(self) -> None:
        """Snapshot the old inventory and check the tree before any archive is fetched.

        ``_load_manifest`` stays a pure fetch (``check_update`` calls it too); this is the
        only place a sync reads state or hashes installed files.
        """
        root = self.install_dir()
        try:
            directdl._check_work_dirs(root)
            plan = self.plan()
            self._strinova_owned = directdl._recorded_members(self._inventory_plan(plan), root)
            self._strinova_missing = self._target_missing(plan)
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        self._strinova_sync = (plan, getattr(self, "_pin_notes", []))

    def _load_manifest(self) -> directdl.Plan:
        held = getattr(self, "_strinova_sync", None)
        self._strinova_sync = None
        if held is None:
            return self.plan()  # an update check: no state, no hashing of installed files
        plan, notes = held
        self._pin_notes = notes
        # A healthy tree needs no archive: an incremental that omits a damaged file would
        # not repair it, and the full set is a whole re-download.
        return replace(plan, archives=()) if not self._strinova_missing else plan

    def _missing_from(self, manifest: directdl.Plan, files: list[Any]) -> list[Any]:
        """Which of ``files`` are still missing or damaged (after a sync)."""
        checks = replace(self._file_plan(manifest), files=tuple(files))
        return directdl.plan_missing(
            checks, self.install_dir(), verify=True, cancel=self.ctx.cancel
        )[0]

    def _repair_with_full_archives(self, manifest: directdl.Plan, missing: list[Any]) -> None:
        """Fetch the full archives while files the incremental did not ship are damaged."""
        if not self._missing_from(manifest, missing):
            return
        previous = (self._read_version_file() or {}).get("version", "")
        # This replaces files: from here an interruption must say RESUME.
        self._write_install_marker((previous, manifest.version))
        full = replace(manifest, archives=manifest.extra["full_archives"])
        directdl.sync(
            full,
            self.install_dir(),
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            workers=directdl.worker_cap(
                full, profile_value(self.ctx.profile, "download_workers")
            ),
        )
        if self._missing_from(manifest, missing):
            raise directdl.DirectError("some game files are still damaged")
        self._install_marker_path().unlink(missing_ok=True)

    @staticmethod
    def _drop_records(records: Any) -> None:
        for record in records:
            for path in (record, directdl._member_list_path(record)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _record_synced_version(self, manifest: directdl.Plan) -> None:
        root = self.install_dir()
        missing = getattr(self, "_strinova_missing", None) or []
        try:
            # Before reading or pruning any record: a linked .nadare would point outside.
            directdl._check_work_dirs(root)
            if missing:
                self._repair_with_full_archives(manifest, missing)
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        super()._record_synced_version(manifest)
        try:
            # Every file now matches the manifest: a launcher patch left part-way is moot.
            strinova_launcher.sync_verified_version(self.prefix(), root, str(manifest.version))
        except Exception as exc:  # noqa: BLE001 - the launcher's bookkeeping never fails a sync
            log.debug("%s: could not update the launcher's config: %s", self.display_name, exc)
        receipt = root / directdl.STATE_DIR / "strinova-inventory.done"
        held = directdl._read_done(receipt) if receipt.exists() else None
        if not missing and held is not None and held.get("version") == manifest.version:
            # Healthy sync: the tree and its receipt are already current. No second scan.
            return
        inventory = self._inventory_plan(manifest)
        try:
            # The new receipt goes first: an interruption must not lose the inventory.
            directdl._write_done(
                inventory,
                inventory.archives[0],
                root,
                [rf.path for rf in self._file_plan(manifest).files],
            )
            records = directdl._superseded_records(inventory, root)
            directdl._prune_dropped_members(
                self._file_plan(manifest),
                root,
                getattr(self, "_strinova_owned", {}),
                records,
            )
            # A fresh install has nothing to prune, so its archive receipts are left; once
            # the inventory receipt owns the whole tree they are redundant.
            self._drop_records(records)
        except OSError as exc:
            # A receipt is bookkeeping: a read-only tree must not turn a finished sync into
            # a failure (the version is already recorded).
            log.debug("%s: could not write the inventory receipt: %s", self.display_name, exc)
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc


publish_game_exports(__name__, globals())
