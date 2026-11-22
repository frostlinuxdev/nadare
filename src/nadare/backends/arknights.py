# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Arknights (Yostar global PC client) backend (``arknights-yostar``).

Nadare installs the game itself, launcher-free: a CI-signed Yostar manifest from Nadare's
index (``yostar/index.json``, see :func:`yostar.configured_manifest`) lists the files, which
:mod:`nadare.core.directdl` downloads and verifies (CRC-64) into the prefix, and Play starts
``Arknights.exe``.

Yostar's own launcher route is retired (#1452, DECISIONS 2026-11-17): with no manifest (the
index turned off and none configured) Install and Update refuse with a clear message; an
installed game still plays. Old ``installer_*`` profile keys are left alone and unused.
It is a :class:`~nadare.backends.direct.DirectBackend` game; what stays Yostar's is the
manifest as the plan (no archive pins, the manifest carries every hash), its own sync messages,
the ``YostarGames/<Game>_EN`` folder, the display/DXVK defaults, news and media. No predownload.


Profile keys (top level or under ``extra``):


``prefix`` / ``wineprefix``
    ``WINEPREFIX`` (default: the profile data dir's ``prefix``).
``install_dir``
    where the game lives (default ``<prefix>/drive_c/YostarGames/Arknights_EN``).
``exe_name`` / ``launch_exe``
    the game executable, relative to ``install_dir`` (default ``Arknights.exe``).
``manifest_index`` / ``manifest_file``
    another manifest source than Nadare's index (``YOSTAR_MANIFEST_INDEX`` wins).
``proton_runner`` / ``runner``
    a Proton runner path or id (``cachyos``/``ge``/``dwproton``); ``wine``,
    ``system`` and ``none`` mean bare Wine.
``wine_binary`` / ``umu_binary``
    Wine and ``umu-run`` executables (autodetected when empty).
``launch_args``
    extra argv (or a shell-word string) appended after the game.
``extra_env``
    extra environment variables.
``process_match``
    extra substring used to detect/stop the running client.

``login`` is not applicable: the account is entered inside the game.
"""


from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any

from ..core import (
    directdl,
    graphics,
    monitors,
    yostar,
)
from ..core.backends import (
    PREFIX_HELP,
    BackendError,
    Option,
)
from ..core.config import profile_value
from ..core.events import Event
from ..core.sandboxed import SANDBOX_EXEMPTION_OPTIONS
from .arknights_news import IMAGE_HOSTS, fetch_arknights_news
from .direct import DirectBackend
from .wine import (
    stream,
)
from .winegame import (
    INSTALL_MARKER,
    INSTALLED_PREFIX_KEY,
    UPDATE_CHECK_INTERVAL,
    VERSION_FILE,
    atomic_write_json,
)

#: Profile kind of this backend.
KIND = "arknights-yostar"

#: Default install tree under the prefix (Yostar's own sub-path; players have games here).
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/Arknights_EN")


log = logging.getLogger(__name__)

#: The paths the last finished sync's manifest listed, under the game folder's state dir.
MANIFEST_FILES = "manifest-files.json"
#: A record bigger than this is not read (a manifest lists a few thousand paths).
_MANIFEST_FILES_LIMIT = 16 * 1024 * 1024


def _clamp_workers(value: Any) -> int:
    """``download_workers`` as the Yostar downloader read it: 1-32, 6 when unreadable."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 6
    return max(1, min(32, number))


class ArknightsBackend(DirectBackend):
    unofficial_notice = True
    install_size_hint = int(3.5 * 1024**3)
    _manifest_errors = (yostar.ManifestError,)

    #: Display driver when the profile says ``auto``: ``wayland`` or ``x11``.
    default_display = "wayland"
    #: Default ``DXVK_CONFIG``. DXVK 3.1.1's VK_EXT_present_timing path makes
    #: the Arknights client hang on its first present (issue #136); older DXVK
    #: ignores the unknown key. A profile ``extra_env`` DXVK_CONFIG overrides it.
    dxvk_config = "dxvk.enablePresentTiming = False"
    kind = KIND
    #: Run the real Microsoft VC++ installer in the prefix. Off by default: it is slow on a
    #: fresh prefix and only Mongil is known to want it (a profile can set ``vcredist: true``).
    vcredist_default = False

    options = (
        Option("prefix", "Wine prefix", "path_dir", PREFIX_HELP),
        Option("install_dir", "Game folder", "path_dir", "Where the game lives."),
        Option("exe_name", "Game executable", "text", advanced=True),
        Option(
            "runner",
            "Runner",
            "runner",
            "Proton build/tag; empty uses the tested DW-Proton (downloaded once).",
        ),
        Option(
            "renderer",
            "Renderer",
            "choice",
            graphics.RENDERER_HELP,
            default="auto",
            choices=graphics.RENDERER_CHOICES,
        ),
        Option(
            "display",
            "Display backend",
            "choice",
            "Wayland is the tested mode. X11 (XWayland) is experimental: on NVIDIA with "
            "KDE Plasma 6.8 the game window shows only a flat colour.",
            default="auto",
            choices=(
                ("Auto (Wayland)", "auto"),
                ("Wayland", "wayland"),
                ("X11 (XWayland)", "x11"),
            ),
        ),
        Option(
            "manifest_index",
            "Manifest index",
            "text",
            "Yostar manifest index URL or path.",
            advanced=True,
        ),
        Option("manifest_file", "Manifest file", "path_file", advanced=True),
        Option(
            "media_file",
            "Media file",
            "path_file",
            "Launcher media document (background/banners).",
            advanced=True,
        ),
        Option(
            "download_workers",
            "Download workers",
            "int",
            "Maximum parallel downloads (1-32); the plan may use fewer.",
            default=32,
            advanced=True,
        ),
        Option("launch_args", "Launch arguments", "args"),
        Option(
            "secure_mode",
            "Sandbox",
            "bool",
            "Hides your home folder from the game (needs bubblewrap). The game can still use "
            "the network. See docs/SECURE-MODE.md.",
            default=True,
        ),
        Option("wine_binary", "Wine binary", "path_file", advanced=True),
        Option("umu_binary", "umu-run binary", "path_file", advanced=True),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        monitors.OPTION,
        Option("process_match", "Process match", "text", advanced=True),
        *SANDBOX_EXEMPTION_OPTIONS,
    )

    #: Game tag used for signed Yostar API calls / manifests.
    game_tag = "Arknights_EN"
    display_name = "Arknights"

    install_subdir: Path = DEFAULT_INSTALL_SUBDIR
    direct_exe: str = "Arknights.exe"
    #: Process detection and Play look for the game itself: a Yostar launcher an old
    #: launcher-route install left in the folder is not the game (#1452).
    launcher_names: tuple[str, ...] = (direct_exe,)

    #: A non-strict runner setup failure, kept so a new prefix never falls back
    #: to a bare ``wine`` that is not installed (or not wanted).
    _runner_setup_error: Exception | None = None

    #: Official Arknights Global news (Yostar's own public API); images come
    #: from the publisher's CDN only.
    news_source = staticmethod(fetch_arknights_news)
    official_image_hosts = IMAGE_HOSTS

    #: The CI media document supplies the launcher hero + operation banners.
    official_media = True

    #: Offered like every direct game (#1450): the game plays launcher-free through
    #: ``_launch_direct``.
    supports_secure_mode = True

    # ---- direct mode (CI-signed manifest) ---------------------------------
    def manifest_source(self) -> str:
        """The configured manifest URL/file, or ``""`` when the index is off."""
        return yostar.configured_manifest(self.ctx.profile)

    def direct_mode(self) -> bool:
        """Always: Yostar's own launcher route is retired (#1452, DECISIONS 2026-11-17)."""
        return True

    def _require_manifest(self) -> str:
        source = self.manifest_source()
        if not source:
            raise BackendError(
                f"{self.display_name} downloads from Nadare's published manifest index, which "
                "is turned off. Set NADARE_INDEX_BASE back, or YOSTAR_MANIFEST_INDEX, or the "
                "profile's 'manifest_index'/'manifest_file'. An installed game still plays."
            )
        return source

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self._require_manifest()  # before anything is created
        super().install()

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self._require_manifest()
        super().update()

    def check_update(self) -> None:
        """No manifest, nothing to compare against: the background check stays quiet."""
        if not self.manifest_source():
            return
        super().check_update()

    def _manifest_error(
        self,
        exc: Exception,
        *,
        changed: bool = False,
        download: bool = False,
        installed: bool = False,
    ) -> BackendError:
        log.warning("invalid %s manifest: %s", self.display_name, exc)
        if download and not changed:
            # The download stopped before any file was replaced (#1452 3b).
            if installed:
                return BackendError(
                    f"{self.display_name} could not finish downloading ({exc}). Nothing was "
                    "changed: the installed game is as it was. Try again later."
                )
            return BackendError(
                f"{self.display_name} could not finish downloading ({exc}). What was already "
                "downloaded is kept: press Install again to continue."
            )
        if changed:
            # A download or check failed part way: some files may already be new (final round).
            return BackendError(
                f"{self.display_name} could not finish downloading ({exc}). Some files may "
                "already be updated: press Update again to finish."
            )
        return BackendError(
            f"{self.display_name}'s download information is invalid or incomplete. "
            "Nothing was changed. Try again later or update Nadare."
        )

    def plan(self) -> directdl.Plan:
        """The CI-signed manifest as a direct-download plan (#1452).

        No archive pins: the manifest already carries every file's hash. A refused host or a
        broken manifest never becomes a plan.
        """
        source = self._require_manifest()  # Repair and Verify say why, like Install (#1452)
        try:
            manifest = yostar.load_manifest(source, game_tag=self.game_tag)
            return yostar.to_plan(manifest, exe=self.direct_exe)
        except yostar.ManifestError as exc:
            raise self._manifest_error(exc) from exc

    # ---- files a newer manifest dropped (#1452) -----------------------------
    def _manifest_files_path(self) -> Path:
        return self.install_dir() / directdl.STATE_DIR / MANIFEST_FILES

    def _read_manifest_files(self) -> set[str]:
        """Paths the last finished sync's manifest listed; empty when absent or unusable.

        No record (an install made before b26) means nothing is known about the folder, so
        nothing is ever removed for it. The record lives in the game folder, which the game
        itself may write: a path is only used when it stays inside that folder, and the
        removal itself takes only regular files reached without a link.
        """
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
                directdl.safe_relative_path(path)
            except directdl.DirectError:
                continue
            usable.add(path)
        return usable

    def _remove_dropped(self, plan: directdl.Plan, before: set[str], owed: list[str]) -> None:
        """After a finished sync: remove what the last record (or an earlier, unfinished
        removal) lists and this plan does not, then record this plan. Never fails the sync."""
        current = {rf.path for rf in plan.files}
        dropped = tuple(sorted((before | set(owed)) - current))
        self._remove_retired(plan, dropped)
        try:
            atomic_write_json(
                self._manifest_files_path(),
                {"game": self.game_tag, "version": str(plan.version), "paths": sorted(current)},
            )
        except OSError as exc:  # a read-only install must not turn a good sync into a failure
            log.debug("%s: could not record the manifest's files: %s", self.display_name, exc)

    def _sync_direct(self) -> dict[str, Any]:
        self._refuse_if_running("installing, updating or repairing it")
        # The host check runs here, before the marker: a refused manifest changes nothing.
        plan = self.plan()
        workers = directdl.worker_cap(plan, profile_value(self.ctx.profile, "download_workers"))
        marker = self._install_marker_path()
        # Read before the sync records the new version: an unfinished removal belongs to it.
        recorded = self._read_manifest_files()
        owed = self._read_leftover_state()
        # A marker already there (an interrupted earlier run) must stay until a sync completes.
        was_marked = marker.exists() or marker.is_symlink()
        # An update cancelled before it replaced a file leaves the complete install usable.
        was_complete = not was_marked and self._read_version_file() is not None
        changed = threading.Event()
        self._write_install_marker()
        self.ctx.emit(Event("step", f"{self.display_name}: syncing {plan.version} directly"))
        try:
            summary = directdl.sync(
                plan,
                self.install_dir(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                workers=_clamp_workers(workers),
                changed=changed,
            )
        except directdl.DirectError as exc:
            if changed.is_set():
                # Files were already replaced: the marker stays and the next sync finishes
                # the job (round 6).
                raise self._manifest_error(exc, changed=True) from exc
            # Nothing was replaced (a failed download, or another sync holds the folder): a
            # complete game stays playable. A marker that was there before (an interrupted
            # earlier run, or the other sync's own) is never dropped.
            if not was_marked and not isinstance(exc, directdl.InstallBusy):
                marker.unlink(missing_ok=True)
            raise self._manifest_error(exc, download=True, installed=was_complete) from exc
        except BaseException:
            if was_complete and not changed.is_set():
                marker.unlink(missing_ok=True)
            raise
        marker.unlink(missing_ok=True)
        self._record_synced_version(plan)
        self._remove_dropped(plan, recorded, owed)
        self.info(
            f"{self.display_name}: synced {summary.get('downloaded', 0)} file(s), "
            f"{summary.get('skipped', 0)} already up to date"
        )
        return summary

__all__ = [
    "KIND",
    "DEFAULT_INSTALL_SUBDIR",
    "INSTALL_MARKER",
    "INSTALLED_PREFIX_KEY",
    "UPDATE_CHECK_INTERVAL",
    "VERSION_FILE",
    "stream",
    "ArknightsBackend",
]
