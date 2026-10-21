# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Stella Sora (Yostar global PC client) backend (``stellasora-yostar``).

Stella Sora ships no usable official Linux installer path here, so this backend
is **direct-download only**: it requires a CI-signed Yostar manifest (see
``ci/yostar-manifests/``) configured through ``YOSTAR_MANIFEST_INDEX`` or the
profile's ``manifest_index`` (URL) / ``manifest_file`` (local path).  Without a
manifest the operations fail with an actionable error instead of guessing.

The Wine/Proton runner handling, prefix discovery, launch-command construction,
process detection and status reporting are inherited from
:class:`~nadare.backends.arknights.ArknightsBackend`, which keeps the shared
Yostar/Wine machinery in one place.  Only the game-specific exe name, install
sub-path, manifest requirement and lifecycle wiring differ.

Profile keys: the same as ``arknights-yostar`` (``prefix``, ``install_dir``,
``exe_name``, ``proton_runner``/``runner``, ``wine_binary``, ``umu_binary``,
``launch_args``, ``extra_env``, ``process_match``) plus the manifest keys.
"""

from __future__ import annotations

from pathlib import Path

from ..core import graphics
from ..core.backends import BackendError, Option
from .arknights import INSTALLED_PREFIX_KEY, ArknightsBackend
from .arknights_news import IMAGE_HOSTS, fetch_stellasora_news

KIND = "stellasora-yostar"

#: Default install tree under the prefix (mirrors Yostar's own sub-path).
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/StellaSora_EN")

#: The game executable a direct install brings and launch runs.
LAUNCHER_NAMES = ("StellaSora.exe",)


class StellaSoraBackend(ArknightsBackend):
    kind = KIND
    game_tag = "StellaSora_EN"
    display_name = "Stella Sora"
    install_subdir = DEFAULT_INSTALL_SUBDIR
    launcher_names = LAUNCHER_NAMES
    direct_exe = "StellaSora.exe"
    #: The gacha swipe (a mouse drag) does not register under Wine's native
    #: Wayland driver but works under XWayland, so default to X11. A profile
    #: with ``display: wayland`` opts back in.
    default_display = "x11"
    dxvk_config = ""  # Stella Sora presents fine with DXVK 3.1.1

    options = (
        Option("prefix", "Wine prefix", "path_dir", "WINEPREFIX used for this game."),
        Option("install_dir", "Game folder", "path_dir", "Where the game lives."),
        Option("exe_name", "Game executable", "text", advanced=True),
        Option("runner", "Runner", "runner", "Proton build/tag; empty uses system Wine."),
        Option(
            "renderer",
            "Renderer",
            "choice",
            graphics.RENDERER_HELP,
            default="auto",
            choices=graphics.RENDERER_CHOICES,
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
            "download_workers",
            "Download workers",
            "int",
            "Parallel downloads (1-16).",
            default=6,
            advanced=True,
        ),
        Option("launch_args", "Launch arguments", "args"),
        Option("wine_binary", "Wine binary", "path_file", advanced=True),
        Option("umu_binary", "umu-run binary", "path_file", advanced=True),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        Option("process_match", "Process match", "text", advanced=True),
        Option(
            "vcredist",
            "Visual C++ runtime",
            "bool",
            "Install the Microsoft Visual C++ runtime into the prefix (recommended; "
            "fixes stutter in some games). A newly created prefix always installs it; "
            "off by default for an existing prefix.",
            default=False,
        ),
    )

    #: Official stellasora.global news (same Yostar CMS as Arknights).
    news_source = staticmethod(fetch_stellasora_news)
    official_image_hosts = IMAGE_HOSTS

    #: Stella Sora launched on Steam on 2026-10-19 (app 4813850, free-to-play).
    page_notice = (
        "Stella Sora is out on Steam (free-to-play, published by Yostar). Playing "
        "it through Steam with Proton is the recommended way; this profile runs "
        "the official client directly.",
        "https://store.steampowered.com/app/4813850/",
        "Steam store page",
    )

    def direct_mode(self) -> bool:
        # Stella Sora is always a direct-manifest game; the manifest itself is
        # still required and checked by install/update/launch below.
        return True

    def _require_manifest(self) -> str:
        source = self.manifest_source()
        if not source:
            raise BackendError(
                "Stella Sora is direct-download only and needs a Yostar manifest: set "
                "YOSTAR_MANIFEST_INDEX or the profile 'manifest_index'/'manifest_file'"
            )
        return source

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self._require_manifest()
        # Record the prefix the install uses so uninstall may later remove an
        # explicitly configured external prefix (and only that one), exactly as
        # ArknightsBackend.install does; this override replaced that base method.
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        self._ensure_direct_prefix()
        self._sync_direct()

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self._require_manifest()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError("Stella Sora is not installed")
        self._sync_direct()

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        self._require_manifest()
        return self._launch_direct()


__all__ = [
    "KIND",
    "DEFAULT_INSTALL_SUBDIR",
    "LAUNCHER_NAMES",
    "StellaSoraBackend",
]
