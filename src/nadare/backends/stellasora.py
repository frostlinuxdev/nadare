# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Stella Sora (Yostar global PC client) backend (``stellasora-yostar``).

Like Arknights it is **direct-download only** (#1452): it requires a CI-signed Yostar
manifest (see ``ci/yostar-manifests/``), from Nadare's published index by default or
through ``YOSTAR_MANIFEST_INDEX`` / the profile's ``manifest_index`` (URL) /
``manifest_file`` (local path). Without one, Install and Update fail with an actionable
error; an installed game still plays.

The Wine/Proton runner handling, prefix discovery, launch-command construction,
process detection and status reporting are inherited from
:class:`~nadare.backends.arknights.ArknightsBackend`, which keeps the shared
Yostar/Wine machinery in one place.  Only the game-specific exe name, install
sub-path, display default, options and news differ.

Profile keys: the same as ``arknights-yostar`` (``prefix``, ``install_dir``,
``exe_name``, ``proton_runner``/``runner``, ``wine_binary``, ``umu_binary``,
``launch_args``, ``extra_env``, ``process_match``) plus the manifest keys.
"""

from __future__ import annotations

from pathlib import Path

from ..core import graphics, monitors
from ..core.backends import PREFIX_HELP, Option
from ..core.sandboxed import SANDBOX_EXEMPTION_OPTIONS
from .arknights import ArknightsBackend
from .arknights_news import IMAGE_HOSTS, fetch_stellasora_news

KIND = "stellasora-yostar"

#: Default install tree under the prefix (mirrors Yostar's own sub-path).
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/StellaSora_EN")

#: The game executable a direct install brings and launch runs.
LAUNCHER_NAMES = ("StellaSora.exe",)


class StellaSoraBackend(ArknightsBackend):
    install_size_hint = 21 * 1024**3
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
            "X11 (XWayland) is the default because the game's swipe works there. "
            "Wayland can be chosen if the display glitches. On NVIDIA with KDE "
            "Plasma 6.8 X11 can show only a flat colour.",
            default="auto",
            choices=(
                ("Auto (X11)", "auto"),
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


__all__ = [
    "KIND",
    "DEFAULT_INSTALL_SUBDIR",
    "LAUNCHER_NAMES",
    "StellaSoraBackend",
]
