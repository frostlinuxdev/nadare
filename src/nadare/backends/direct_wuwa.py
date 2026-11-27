# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Wuthering Waves, split from direct_games to keep both under the size limit (#1601)."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..core import (
    diskspace,
    installlock,
    removal,
)
from ..core.backends import BackendError, Option, Status
from ..core.gameversion import versions_equal
from ..core.sources import (
    wuwa,
)
from . import (
    kuro_news,
)
from .direct import GB, DirectBackend, game_subdir, log, publish_game_exports
from .wine import profile_setting
from .winegame import atomic_write_json


class WutheringWavesBackend(DirectBackend):
    kind = "direct-wuwa"
    #: The runner's WuWa fixes (gamefixes-umu/umu-3513350.py: fonts, no SteamOS/SteamDeck,
    #: the bitmap flag that ends 3.8's white web pages, one ``-krqlv``) only run under this id;
    #: with ``0`` they never did (#1334). A profile ``extra_env`` GAMEID still wins.
    umu_id = "umu-3513350"
    news_source = staticmethod(kuro_news.fetch_wuwa_news)
    official_image_hosts = kuro_news.IMAGE_HOSTS
    game_tag = "wuwa"
    display_name = "Wuthering Waves"
    tidies_retired = True
    #: On native Wayland a player's sign-in window opened on another screen; XWayland keeps the
    #: game's windows together (2026-11-12). ``display: wayland`` still wins.
    default_display = "x11"
    install_subdir = game_subdir("WutheringWaves")
    direct_exe = "Wuthering Waves.exe"
    launcher_names = (direct_exe,)
    #: The game itself, for the launch sheet only: the stub above is never started by
    #: Nadare. Not ``game_process_names``, which also drives Flatpak running detection.
    first_frame_process_names = ("Client-Win64-Shipping.exe",)
    install_size_hint = int(85.9 * GB)
    plan_factory = staticmethod(wuwa.resolve)
    supports_predownload = True
    predownload_factory = staticmethod(wuwa.resolve_predownload)
    # Asked in the install confirm (#990), with the sizes from the option's help.
    install_choice = "wuwa_quality"
    install_choice_sizes = {"sd": 58 * 2**30, "hd": 80 * 2**30, "uhd": 99 * 2**30}
    options = DirectBackend.options + (
        Option(
            "wuwa_quality",
            "Resource quality",
            "choice",
            "The size of the game's textures, as in Kuro's launcher: SD about 58 GiB, HD about "
            "80 GiB, UHD about 99 GiB. Changing it downloads the missing files on the next "
            "Update; the folder of the old quality stays until you delete it.",
            default="hd",
            choices=(("SD", "sd"), ("HD", "hd"), ("UHD", "uhd")),
        ),
        Option(
            "kuro_token",
            "Kuro download token",
            "text",
            "Only needed if Kuro rotates the token Nadare ships (see core/sources/wuwa.py).",
            advanced=True,
        ),
    )

    def _latest_variant(self) -> str:
        # Each quality (and a custom download token) has its own published version (#975); the
        # default HD with the shipped token keeps the plain cache name.
        quality = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        parts = [] if quality == wuwa.DEFAULT_QUALITY else [quality]
        if str(self.ctx.profile.get("kuro_token") or "").strip():
            parts.append("token")  # never the token itself
        return "-".join(parts)

    def plan_kwargs(self) -> dict[str, Any]:
        quality = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        kwargs: dict[str, Any] = {"quality": quality}
        token = str(self.ctx.profile.get("kuro_token") or "").strip()
        if token:
            kwargs["token"] = token
        return kwargs

    def _quality_folder(self, quality: str) -> Path:
        return self.install_dir() / "Client" / "Content" / quality.upper()

    def other_quality_folders(self) -> list[tuple[str, Path, int]]:
        """The resource tiers this profile no longer uses, with their sizes (#990).

        Each tier is its own folder under ``Client/Content``. Nothing is offered until the
        chosen tier has files: before its update downloads them, the old tier is still the
        one the game plays. Symlinked folders are never followed.
        """
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        chosen_folder = self._quality_folder(chosen)

        root = self.install_dir()

        def safe(folder: Path) -> bool:
            """A real folder inside the install dir with no symlink below the install dir.

            Links above it are fine (``/home`` is a link to ``/var/home`` on Silverblue/Bazzite).
            """
            try:
                inside = folder.relative_to(root)
                return folder.resolve() == root.resolve() / inside
            except (OSError, ValueError):
                return False

        if not safe(chosen_folder) or not chosen_folder.is_dir():
            return []
        if self._install_marker_path().exists() or chosen not in self._complete_tiers():
            # Empty or partial (a cancelled Update): the other tier is still the one that plays,
            # so it must never be offered for deletion (#1335).
            return []
        found: list[tuple[str, Path, int]] = []
        for quality in wuwa.QUALITIES:
            if quality == chosen:
                continue
            folder = self._quality_folder(quality)
            if not safe(folder) or not folder.is_dir():
                continue
            size = diskspace.tree_bytes(folder)
            if size <= 0:
                continue  # nothing to free; a "0 B no longer needed" prompt helps no one
            found.append((quality, folder, size))
        return found

    def remove_other_qualities(self, only: Iterable[str] | None = None) -> tuple[int, list[str]]:
        """Delete the unused resource tiers, never while the game runs (#990).

        ``only`` restricts the deletion to the tiers the user confirmed; each is still
        re-checked through :meth:`other_quality_folders` (symlinks and the chosen tier).
        """
        self._refuse_if_running("removing files")
        # The same lock a download, update or predownload holds while it writes here: the
        # cleanup runs on a pool thread and must never delete under a running sync (#1474).
        with installlock.hold(
            self.install_dir(),
            BackendError,
            f"{self.display_name} is being downloaded or updated right now; remove the other "
            "quality when that has finished.",
        ):
            return self._remove_other_qualities_locked(only)

    def _remove_other_qualities_locked(self, only: Iterable[str] | None) -> tuple[int, list[str]]:
        wanted = None if only is None else {str(name).lower() for name in only}
        freed = 0
        warnings: list[str] = []
        removed: list[str] = []
        for quality, folder, size in self.other_quality_folders():
            if wanted is not None and quality not in wanted:
                continue
            try:
                outcome = removal._rmtree_safely(folder)
            except OSError as exc:
                warnings.append(removal._leftover_warning(folder, exc))
                continue
            for mount in outcome.mounts:
                warnings.append(f"{mount} is a separate mounted drive and was left alone")
            for path, exc in outcome.leftovers:
                warnings.append(removal._leftover_warning(path, exc))
            if not folder.exists():
                freed += size
                removed.append(quality)
        recorded = self._recorded_tiers()
        if removed and recorded is not None:
            try:
                self._write_recorded_tiers({q: v for q, v in recorded.items() if q not in removed})
            except OSError as exc:
                log.debug("%s: could not update the tier record: %s", self.display_name, exc)
        return freed, warnings

    #: The game binary. ``Wuthering Waves.exe`` (``direct_exe``) is Kuro's launcher stub: it
    #: runs ``ACE-Setup64.exe``, which installs the ACE kernel driver into the prefix, and Wine
    #: aborts on the kernel functions that driver calls. Starting the game binary itself
    #: reaches the game without that step (Twintail Launcher starts it the same way).
    GAME_EXE = "Client/Binaries/Win64/Client-Win64-Shipping.exe"

    def direct_target(self) -> Path | None:
        if profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        ):
            return super().direct_target()
        return self.install_dir() / self.GAME_EXE

    launch_extra_args_before_user = True

    def launch_extra_args(self) -> list[str]:
        # The launcher stub passes these; a profile that sets its own launch_args keeps control.
        if self._extra_args("launch_args"):
            return []
        return ["-dx12", f"-krqlv={self._launch_quality()}"]

    def _tier_installed(self, quality: str) -> bool:
        """Whether ``Client/Content/<TIER>`` holds at least one non-empty file."""
        folder = self._quality_folder(quality)
        try:
            for _dirpath, _dirs, files in os.walk(folder):
                for name in files:
                    if name.endswith(".part") or ".nadare-new" in name:
                        continue  # a download still in progress or cancelled (stress F1)
                    with contextlib.suppress(OSError):
                        if (Path(_dirpath) / name).stat().st_size > 0:
                            return True
        except OSError:
            return False
        return False

    def _recorded_tiers(self) -> dict[str, str] | None:
        """``{tier: version}`` a finished sync recorded (``qualities``); ``None`` when unusable.

        Each tier keeps the game version it was last synced at: an update downloads only the
        chosen tier, so the others stay at their own version (stress round 2, r3 F2). A list
        (the first #1335 build) means every listed tier is at the recorded version. A record
        with no known tier is treated like an old version file (r1: it switched off both the
        pending update and the Repair refusal).
        """
        record = self._read_version_file()
        if not isinstance(record, dict):
            return None
        listed = record.get("qualities")
        current = str(record.get("version") or "")
        found: dict[str, str] = {}
        if isinstance(listed, list):
            names = {str(x).strip().lower() for x in listed}
            found = {q: current for q in wuwa.QUALITIES if q in names}
        elif isinstance(listed, dict):
            lowered = {str(k).strip().lower(): str(v) for k, v in listed.items()}
            found = {q: lowered[q] for q in wuwa.QUALITIES if q in lowered}
        return found or None

    def _complete_tiers(self) -> list[str]:
        """Tiers fully installed at the current game version, and still on disk.

        A version file from before #1335 has no record; then every tier with real files counts,
        as #1333 did, except the chosen one while a sync is unfinished (it may be the partial
        one). A tier only partly downloaded is never recorded: the record is written from the
        disk before the first sync starts (:meth:`_before_sync`), and a sync adds only its own
        tier.
        """
        recorded = self._recorded_tiers()
        if recorded is None:
            candidates = list(wuwa.QUALITIES)
            if self._install_marker_path().exists():
                chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
                candidates.remove(chosen)
        else:
            record = self._read_version_file() or {}
            current = str(record.get("version") or "")
            candidates = [
                q for q in wuwa.QUALITIES if q in recorded and versions_equal(recorded[q], current)
            ]
        return [q for q in candidates if self._tier_installed(q)]

    def _write_recorded_tiers(self, tiers: Mapping[str, str]) -> None:
        path = self._version_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        data["qualities"] = {q: str(tiers[q]) for q in wuwa.QUALITIES if q in tiers}
        atomic_write_json(path, data)

    def _before_sync(self) -> None:
        """Turn an old version file's disk guess into a record before anything downloads.

        Otherwise a tier download cancelled half way would be on disk when the next sync
        finishes, and the disk guess would record it as complete (stress round 2, r2).
        """
        if self._recorded_tiers() is not None:
            return
        record = self._read_version_file()
        version = str(record.get("version") or "") if isinstance(record, dict) else ""
        if not version:
            return  # a first install: nothing was complete before it
        try:
            self._write_recorded_tiers({q: version for q in self._complete_tiers()})
        except OSError as exc:
            log.debug("%s: could not record the tiers: %s", self.display_name, exc)

    def _record_synced_version(self, manifest: Any) -> None:
        """Record the tier this sync completed at its version; other tiers keep theirs."""
        before = dict(self._recorded_tiers() or {})  # read before the base class rewrites it
        super()._record_synced_version(manifest)
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        if self._tier_installed(chosen):  # r1: never a tier the sync left without files
            before[chosen] = str(manifest.version)
        try:
            self._write_recorded_tiers(before)
        except OSError as exc:
            log.debug("%s: could not record the tier: %s", self.display_name, exc)

    def _tidy_allowed(self) -> bool:
        """No Kuro tidy while the chosen tier is pending: its plan would call the tier that
        plays "retired" (stress round 2, r3 F3)."""
        complete = self._complete_tiers()
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        return chosen in complete or not complete

    def _tidy_names(self, names: Iterable[str]) -> list[str]:
        """Never tidy inside another tier's folder; that is the cleanup offer's job (#990)."""
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        others = tuple(f"client/content/{q}/" for q in wuwa.QUALITIES if q != chosen)
        return [
            n for n in names if not str(n).replace("\\", "/").lstrip("/").lower().startswith(others)
        ]

    def _direct_status_parts(
        self, target: Path | None, incomplete: bool
    ) -> tuple[dict[str, Any], str | None, Status | None]:
        additions, version, early = super()._direct_status_parts(target, incomplete)
        if not version:
            return additions, version, early
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        if early is not None:
            interrupted = self._interrupted_update() if incomplete else None
            if (
                interrupted is not None
                and versions_equal(str(interrupted[0]), str(interrupted[1]))
                and chosen not in self._complete_tiers()
            ):
                # Same version before and after: it was a quality download, not a game update
                # (stress round 2, r2: the page said "update to 3.8.0 was interrupted").
                additions["pending_quality"] = chosen
                early = dataclasses.replace(
                    early,
                    message=(
                        f"downloading the {chosen.upper()} files was interrupted — click "
                        "RESUME to finish it; only what is missing is downloaded"
                    ),
                )
            return additions, version, early
        complete = self._complete_tiers()
        if chosen not in complete and (complete or self._recorded_tiers()):
            # A switched quality is an update: the version is the same for every tier, so the
            # version check alone said "up to date" and the tier was never downloaded (#1335).
            # Name the setting: a profile set to SD by the old Settings bug (#1332) can be set
            # back instead of downloading a tier the player never chose.
            additions["update_available"] = True
            additions["pending_quality"] = chosen
            additions["latest_version"] = f"{chosen.upper()} files (Resource quality in Settings)"
        return additions, version, early

    def _launch_quality(self) -> str:
        """The tier to start: the chosen one, or an installed one until Update brings it.

        ``-krqlv`` naming a tier whose files were never downloaded crashes the client ("Fatal
        error", 2026-11-14). The saved choice is left alone, so Update still downloads it.
        With no tier folder at all there is nothing to compare, so the choice stands.
        """
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        complete = self._complete_tiers()
        if chosen in complete:
            return chosen
        if not complete:
            if self._recorded_tiers():
                # Tiers were installed and are all gone (deleted by hand or a cleanup tool):
                # starting would crash on files that are not there (stress W2).
                raise BackendError(
                    f"The {chosen.upper()} resource files are missing from the game folder. "
                    "Press Repair to download them again."
                )
            return chosen  # an older record with no tier folders: nothing to compare
        # HD first (the default and the usual install), then the others in list order.
        order = [wuwa.DEFAULT_QUALITY] + [q for q in wuwa.QUALITIES if q != wuwa.DEFAULT_QUALITY]
        installed = next(q for q in order if q in complete)
        from ..core.events import Event

        note = (
            f"{chosen.upper()} files are not downloaded yet, so the game starts with "
            f"{installed.upper()}. Press Update to download {chosen.upper()}."
        )
        log.info("WuWa: %s", note)
        self.ctx.emit(Event("step", f"{self.display_name}: {note}"))
        return installed


publish_game_exports(__name__, globals())
