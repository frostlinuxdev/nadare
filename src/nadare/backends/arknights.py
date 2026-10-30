# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Arknights (Yostar global PC client) backend (``arknights-yostar``).


The official global PC client is a Windows program distributed by Yostar
(``Arknights_EN_Gamelauncher-*-setup.exe``).  It is installed and run inside a
Wine/Proton prefix, following the community guide that uses Proton-CachyOS
(or DW-Proton): download the setup, run it in the prefix, then run the installed
launcher from the same prefix.  The launcher itself handles
downloading, updating and starting the actual game.


Profile keys (top level or under ``extra``):


``prefix`` / ``wineprefix``
    ``WINEPREFIX`` (default: the profile data dir's ``prefix``).
``install_dir``
    where the launcher lives (default ``<prefix>/drive_c/YostarGames/Arknights_EN``,
    matching the launcher's own ``YostarGames/Arknights_EN`` default sub-path).
``exe_name`` / ``launch_exe``
    the launcher executable, relative to ``install_dir``.  When unset the
    backend looks for a known launcher name and otherwise walks the prefix.
``installer_url`` / ``installer``
    a direct ``https://…/…-setup.exe`` link.  When unset the backend asks the
    official download API and falls back to the pinned URL below.
``installer_sha256``
    expected SHA-256 of the installer ``.exe``.  When set the download is
    verified (and the host restriction is relaxed, since the bytes are pinned).
    When unset the installer URL must be on Yostar's own hosts.
``installer_args``
    extra argv (or a shell-word string) for the installer, e.g. ``/S /D=C:\\…``.
``proton_runner`` / ``runner``
    a Proton runner path or id (``cachyos``/``ge``/``dwproton``); ``wine``,
    ``system`` and ``none`` mean bare Wine.
``wine_binary`` / ``umu_binary``
    Wine and ``umu-run`` executables (autodetected when empty).
``launch_args``
    extra argv (or a shell-word string) appended after the launcher.
``extra_env``
    extra environment variables.
``process_match``
    extra substring used to detect/stop the running client.


The launcher performs its own updates, so :meth:`update` simply runs it.
``login`` is not applicable: the account is entered inside the game.


TODO: Yostar rotates both the installer URL and its filename; the download API
(``INSTALLER_API``) is the stable way to find the current link, and the pinned
``DEFAULT_INSTALLER_URL`` is only a last resort that will eventually go stale.
"""


from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core import (
    graphics,
    yostar,
)
from ..core.backends import (
    BackendError,
    Option,
)
from ..core.events import Event
from .arknights_news import IMAGE_HOSTS, fetch_arknights_news
from .wine import (
    profile_setting,
    stream,
)
from .winegame import (
    DEFAULT_INSTALL_SUBDIR,
    DEFAULT_INSTALLER_URL,
    INSTALL_MARKER,
    INSTALLED_PREFIX_KEY,
    INSTALLER_API,
    KIND,
    LAUNCHER_NAMES,
    UPDATE_CHECK_INTERVAL,
    VERSION_FILE,
    WineGameBackend,
)

log = logging.getLogger(__name__)


class ArknightsBackend(WineGameBackend):
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
        Option("prefix", "Wine prefix", "path_dir", "WINEPREFIX used for this game."),
        Option("install_dir", "Game folder", "path_dir", "Where the launcher/game lives."),
        Option("exe_name", "Launcher executable", "text", advanced=True),
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
            "Parallel downloads (1-16).",
            default=6,
            advanced=True,
        ),
        Option("installer_url", "Installer URL", "text", advanced=True),
        Option(
            "installer_sha256",
            "Installer SHA-256",
            "text",
            "Expected SHA-256 of the installer .exe (optional; pins the bytes).",
            advanced=True,
        ),
        Option("installer_args", "Installer arguments", "args", advanced=True),
        Option("launch_args", "Launch arguments", "args"),
        Option("wine_binary", "Wine binary", "path_file", advanced=True),
        Option("umu_binary", "umu-run binary", "path_file", advanced=True),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        Option("process_match", "Process match", "text", advanced=True),
    )

    #: Game tag used for signed Yostar API calls / manifests.
    game_tag = "Arknights_EN"
    display_name = "Arknights"

    #: Default install sub-path and launcher candidates, overridable so the
    #: Stella Sora backend can reuse the same Wine/runner machinery.
    install_subdir: Path = DEFAULT_INSTALL_SUBDIR
    launcher_names: tuple[str, ...] = LAUNCHER_NAMES
    direct_exe: str = "Arknights.exe"
    #: Installer defaults and trust, overridable by launcher-only games.
    default_installer_url: str = DEFAULT_INSTALLER_URL
    installer_api: str | None = INSTALLER_API

    #: A non-strict runner setup failure, kept so a new prefix never falls back
    #: to a bare ``wine`` that is not installed (or not wanted).
    _runner_setup_error: Exception | None = None

    #: Official Arknights Global news (Yostar's own public API); images come
    #: from the publisher's CDN only.
    news_source = staticmethod(fetch_arknights_news)
    official_image_hosts = IMAGE_HOSTS

    #: The CI media document supplies the launcher hero + operation banners.
    official_media = True

    # ---- paths / profile resolution ---------------------------------------

    # ---- installer URL -----------------------------------------------------

    def _installer_host_trusted(self, url: str) -> bool:
        """Whether an unpinned installer may come from ``url``'s host (Yostar's)."""
        return yostar.base_url_trusted(url)

    # ---- direct mode (CI-signed manifest) ---------------------------------
    def manifest_source(self) -> str:
        """The configured manifest URL/file, or ``""`` for the official path."""
        return yostar.configured_manifest(self.ctx.profile)

    def direct_mode(self) -> bool:
        return bool(self.manifest_source())

    def _manifest_error(self, exc: yostar.ManifestError) -> BackendError:
        log.warning("invalid %s manifest: %s", self.display_name, exc)
        return BackendError(
            f"{self.display_name}'s download information is invalid or incomplete. "
            "Nothing was changed. Try again later or update Nadare."
        )

    def _load_manifest(self) -> yostar.Manifest:
        try:
            return yostar.load_manifest(self.manifest_source(), game_tag=self.game_tag)
        except yostar.ManifestError as exc:
            raise self._manifest_error(exc) from exc

    # ---- recorded / published versions ------------------------------------

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        """Files a direct install still lacks or has damaged (each has ``.path``).

        ``verify=False`` checks presence and size only (the caller just synced and hashed them).
        """
        return list(yostar.plan(self._load_manifest(), self.install_dir(), verify=verify))

    def _sync_direct(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        workers = profile_setting(self.ctx.profile, "download_workers", 6)
        marker = self._install_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("sync in progress\n", encoding="utf-8")
        self.ctx.emit(Event("step", f"{self.display_name}: syncing {manifest.version} directly"))
        try:
            summary = yostar.sync(
                manifest,
                self.install_dir(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                workers=workers,
            )
        except yostar.ManifestError as exc:
            raise self._manifest_error(exc) from exc
        marker.unlink(missing_ok=True)
        self._record_synced_version(manifest)
        self.info(
            f"{self.display_name}: synced {summary.get('downloaded', 0)} file(s), "
            f"{summary.get('skipped', 0)} already up to date"
        )
        return summary

    # ---- launch-time additions (login tokens and the like) -----------------

    # ---- launcher discovery ------------------------------------------------

    # ---- command construction ---------------------------------------------


__all__ = [
    "KIND",
    "INSTALLER_API",
    "DEFAULT_INSTALLER_URL",
    "DEFAULT_INSTALL_SUBDIR",
    "INSTALL_MARKER",
    "INSTALLED_PREFIX_KEY",
    "LAUNCHER_NAMES",
    "UPDATE_CHECK_INTERVAL",
    "VERSION_FILE",
    "stream",
    "ArknightsBackend",
]
