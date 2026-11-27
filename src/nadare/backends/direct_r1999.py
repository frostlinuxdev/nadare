# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct installation of Reverse: 1999."""

from __future__ import annotations

import os
import stat
from typing import Any

from ..core import directdl
from ..core.backends import BackendError, BackendNotInstalled
from ..core.events import Event
from ..core.sources import r1999
from .direct import GB, DirectBackend, game_subdir, publish_game_exports
from .webview2_signin import WebView2SignInMixin


class R1999Backend(WebView2SignInMixin, DirectBackend):
    kind = "direct-r1999"
    game_tag = "r1999"
    display_name = "Reverse: 1999"
    install_subdir = game_subdir("Reverse1999")
    direct_exe = r1999.EXE
    launcher_names = (direct_exe,)
    install_size_hint = int(33.2 * GB)
    plan_factory = staticmethod(r1999.resolve)
    sign_in_windows_version = "win7"

    #: Set while Repair runs: it re-syncs the full package, whatever version is installed.
    _force_full = False
    #: The plan the last sync loaded, for Repair's after-sync check.
    _loaded_plan: directdl.Plan | None = None

    def plan_kwargs(self) -> dict[str, Any]:
        if self._force_full or self._resuming_a_repair():
            return {"current_version": ""}
        record = self._read_version_file()
        return {"current_version": str(record["version"]) if record else ""}

    def _resuming_a_repair(self) -> bool:
        """An unfinished sync that was not an update from an older version (#1827).

        A Repair re-syncs the installed version ("update from N to N" in the marker). Asked
        with that version, the feed says there is nothing to do and RESUME would mark a
        half-repaired tree installed; the full package is what finishes it. An interrupted
        update (N-1 to N) still resumes with its delta.
        """
        if self._force_full or not self._install_incomplete():
            return False
        record = self._read_version_file()
        if record is None:
            return False  # a first install: no current version is asked for anyway
        interrupted = self._interrupted_update()
        if interrupted is None:
            return True  # nothing says it was a delta update: the full package is safe
        old, new = interrupted
        return directdl.versions_equal(old, new) or not directdl.versions_equal(
            old, str(record["version"])
        )

    def update(self) -> None:
        """Update with the diff package, or the full one when installed files are gone (#1836).

        A diff carries only the files that changed, so a file the player (or an antivirus)
        removed would stay missing. When any file an earlier archive installed is missing or
        has another size, take the full package, as Repair does.
        """
        missing = self._installed_files_missing() if self.install_dir().is_dir() else []
        if not missing:
            super().update()
            return
        names = ", ".join(missing[:3])
        if len(missing) > 3:
            names += f" (+{len(missing) - 3} more)"
        self.ctx.emit(
            Event(
                "step",
                f"{self.display_name}: {len(missing)} game file(s) are missing or damaged "
                f"({names}); downloading the full package instead of the update",
            )
        )
        self._force_full = True
        try:
            super().update()
        finally:
            self._force_full = False

    def _installed_files_missing(self) -> list[str]:
        """Files the recorded archives installed that are gone, not regular files, or resized.

        Receipts are read in the order they were applied, so a file a delta replaced is
        checked against the delta's size. A receipt cut at the member cap is checked for
        presence only (its member list).
        """
        root = self.install_dir()
        state = root / directdl.STATE_DIR
        records = []
        try:
            candidates = sorted(state.rglob("*.done"))
        except OSError:
            return []
        for record_path in candidates:
            record = directdl._read_done(record_path)
            if record is None or record.get("game", self.game_tag) != self.game_tag:
                continue
            records.append((directdl._record_order(record_path, record), record_path, record))
        expected: dict[str, int | None] = {}
        missing: list[str] = []
        for _, record_path, record in sorted(records, key=lambda item: item[0]):
            if record.get("members_truncated"):
                if not directdl._all_members_present(record_path, root):
                    missing.append(record_path.name.removesuffix(".done"))
                continue
            members, sizes = record.get("members"), record.get("member_sizes")
            if not isinstance(members, list):
                continue
            for member in members:
                if not isinstance(member, str):
                    continue
                size = sizes.get(member) if isinstance(sizes, dict) else None
                expected[member] = size if isinstance(size, int) else None
        for member, size in sorted(expected.items()):
            try:
                info = os.lstat(root / directdl.safe_relative_path(member))
            except (OSError, directdl.DirectError):
                missing.append(member)
                continue
            if not stat.S_ISREG(info.st_mode) or (size is not None and info.st_size != size):
                missing.append(member)
        return missing

    def _load_manifest(self) -> directdl.Plan:
        # Record what the sync itself used (plan() may be replaced, e.g. by a cached plan).
        plan = super()._load_manifest()
        self._loaded_plan = plan
        return plan

    def repair(self) -> None:
        """Re-install the full package, then check it.

        The feed has no complete file list, so Repair cannot compare files one by one.
        The full package is the one complete, verifiable plan there is: its archives carry
        exact sizes and MD5s. Force it (even for an up-to-date delta install), then check
        that every archive of that same plan is installed.
        """
        if not self.install_dir().is_dir():
            raise BackendNotInstalled(f"{self.display_name} is not installed")
        self._force_full = True
        self._loaded_plan = None
        try:
            self._sync_direct()
        finally:
            self._force_full = False
        plan = self._loaded_plan
        if plan is None:
            raise BackendError(
                f"{self.display_name}: could not read the publisher's full download information"
            )
        try:
            files, archives = directdl.plan_missing(
                plan, self.install_dir(), verify=False
            )
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        missing = [rf.path for rf in files] + [archive.name for archive in archives]
        if missing:
            names = ", ".join(missing[:5])
            if len(missing) > 5:
                names += f" (+{len(missing) - 5} more)"
            raise BackendError(
                f"{len(missing)} game file(s) are still damaged: {names}. "
                "Try again, or reinstall the game."
            )
        self.ctx.emit(Event("done", "All game files check out"))

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        # The feed supplies archives, not a complete manifest of the updated install.
        # Base receipts have older versions and hashes for files replaced by deltas.
        raise BackendError(
            "Verification is unavailable: the publisher does not provide a complete file list."
        )


publish_game_exports(__name__, globals())
