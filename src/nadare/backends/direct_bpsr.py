# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Blue Protocol: Star Resonance (global, A Plus) direct backend (#1783)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from ..core import directdl
from ..core.backends import BackendError
from ..core.diskspace import free_needed, human_size
from ..core.sources import bpsrglobal
from .direct import DirectBackend, game_subdir, log, publish_game_exports
from .winegame import atomic_write_json

#: Managed file inventory from the last finished sync, under ``<install>/.nadare/``.
MANIFEST_FILES = "manifest-files.json"
_MANIFEST_FILES_LIMIT = 16 * 1024 * 1024

#: ``game_mini.zip`` on the fresh-install path: what the progress bar totals.
DOWNLOAD_BYTES = 42_772_592_920
#: The finished tree, summed from md5list.txt's 9,719 rows.
INSTALLED_BYTES = 47_980_628_350
#: Peak free space: the zip stays on disk while it unpacks to the tree above.
PEAK_BYTES = DOWNLOAD_BYTES + INSTALLED_BYTES


class BpsrBackend(DirectBackend):
    kind = "direct-bpsr"
    game_tag = "bpsr"
    display_name = "Blue Protocol: Star Resonance"
    install_subdir = game_subdir("BlueProtocolStarResonance")
    direct_exe = bpsrglobal.EXE
    launcher_names = (direct_exe,)
    #: The finished install (the Store's figure); the zip is gone by then.
    install_size_hint = INSTALLED_BYTES
    download_size_hint = DOWNLOAD_BYTES
    #: Peak free space during a fresh install: the zip plus the tree it unpacks to.
    install_space_hint = PEAK_BYTES
    plan_factory = staticmethod(bpsrglobal.resolve)
    page_notice = (
        "Global (A Plus) build, downloaded straight from the publisher's servers. "
        f"It is about {human_size(DOWNLOAD_BYTES)} to download and "
        f"{human_size(INSTALLED_BYTES)} installed; keep about "
        f"{human_size(free_needed(PEAK_BYTES))} free while unpacking. The game ships "
        "Anti-Cheat Expert (ACE) and may not run under Wine/Proton. Blue Protocol: Star "
        "Resonance is also on Steam (app 3681810), which is the easier route if you use Steam.",
        "https://store.steampowered.com/app/3681810/",
        "Steam page",
    )

    #: Inventory read before a sync, so the dropped-path set is the one that was installed
    #: before the new version recorded itself (``_read_leftover_state`` is version-scoped).
    _inventory_before: set[str] | None = None
    _owed_before: list[str] | None = None

    def _manifest_files_path(self) -> Path:
        return self.install_dir() / directdl.STATE_DIR / MANIFEST_FILES

    def _check_state_dirs(self) -> None:
        """Refuse a linked or invalid state folder before touching the inventory."""
        try:
            directdl._check_work_dirs(self.install_dir())
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc

    def _read_manifest_files(self) -> set[str]:
        """Paths listed by the last completed md5list; empty when absent, tampered or unsafe."""
        self._check_state_dirs()
        try:
            fd = os.open(self._manifest_files_path(), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return set()
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return set()
                raw = handle.read(_MANIFEST_FILES_LIMIT + 1)
        except OSError:
            return set()
        if len(raw) > _MANIFEST_FILES_LIMIT:
            return set()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return set()
        if not isinstance(payload, dict) or payload.get("game") != self.game_tag:
            return set()
        paths = payload.get("paths")
        if not isinstance(paths, list):
            return set()
        usable: set[str] = set()
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            try:
                normalised = directdl.safe_relative_path(path)
            except directdl.DirectError:
                continue
            usable.add(normalised)
        return usable

    def _before_sync(self) -> None:
        """Read the old inventory and leftover names before the new version is recorded."""
        self._check_state_dirs()
        self._inventory_before = self._read_manifest_files()
        self._owed_before = self._read_leftover_state()

    def _is_fresh_install(self) -> bool:
        """True when no finished sync has recorded a version and a file inventory yet.

        Whether BPSR.exe exists says nothing: it is early in the real zip, so a killed fresh
        install already has it. No version record and no inventory means the zip path; the
        install marker and a kept archive are evidence of that same state.
        """
        if self._read_version_file() is not None:
            return False
        return not self._read_manifest_files()

    def _remove_stale_archive(self) -> None:
        """Delete a game_mini.zip (and partial copies) a killed fresh install left behind."""
        dl_dir = self.install_dir() / directdl.DL_DIR
        for name in ("game_mini.zip", "game_mini.zip.part", "game_mini.zip.nadare-new"):
            try:
                path = directdl._cache_path(dl_dir, name)
            except directdl.DirectError as exc:
                raise BackendError(f"{self.display_name}: {exc}") from exc
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode):
                try:
                    path.unlink()
                except OSError as exc:
                    log.debug(
                        "%s: could not remove the stale archive: %s", self.display_name, exc
                    )

    def _direct_extra_plan(self, plan: directdl.Plan) -> directdl.Plan | None:
        """A fresh install takes the single zip; anything else must not keep a stale one."""
        if not self._is_fresh_install():
            self._remove_stale_archive()
            return None
        archive_url = plan.extra.get("archive_url") if isinstance(plan.extra, Mapping) else None
        if not archive_url:
            # Only a synthetic plan (tests) lacks this; a resolved one always carries it.
            log.debug("%s: plan has no archive URL; using the per-file path", self.display_name)
            return None
        return bpsrglobal.fresh_archive_plan(plan)

    def _remove_retired(self, plan: directdl.Plan, retired: tuple[str, ...]) -> None:
        """BPSR's md5list is its inventory: drop the paths the current one no longer lists."""
        self._remove_dropped(plan)

    def _remove_dropped(self, plan: directdl.Plan) -> None:
        """Delete paths listed by the previous md5list that the new one dropped, then save."""
        current = {rf.path for rf in plan.files}
        current_lower = {path.lower() for path in current}
        before = self._inventory_before
        if before is None:
            before = self._read_manifest_files()
        owed = self._owed_before
        if owed is None:
            owed = self._read_leftover_state()
        dropped = tuple(sorted(p for p in (before | set(owed)) if p.lower() not in current_lower))
        super()._remove_retired(plan, dropped)
        self._check_state_dirs()
        try:
            atomic_write_json(
                self._manifest_files_path(),
                {"game": self.game_tag, "version": str(plan.version), "paths": sorted(current)},
            )
        except OSError as exc:
            log.debug("%s: could not record the manifest's files: %s", self.display_name, exc)


publish_game_exports(__name__, globals())
