# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Heaven Burns Red (English), installed from the shared Yostar manifest feed."""

from pathlib import Path

from .arknights import ArknightsBackend
from .direct import DirectBackend

KIND = "hbr-yostar"
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/HeavenBurnsRed_EN")


class HbrBackend(ArknightsBackend):
    kind = KIND
    game_tag = "HBR_EN"
    display_name = "Heaven Burns Red"
    install_subdir = DEFAULT_INSTALL_SUBDIR
    direct_exe = "HeavenBurnsRed.exe"
    launcher_names = (direct_exe,)
    install_size_hint = 38_706_311_237
    # No game-specific display or presentation workaround has been verified.
    default_display = "x11"
    # The Yostar feed's number is the package Nadare downloads; HBR's own title screen shows
    # its build (4.0.2(72) while the feed said 4.0.3, real run 11-24). Say which is which.
    version_is_download = True
    dxvk_config = ""
    options = DirectBackend.options + tuple(
        option
        for option in ArknightsBackend.options
        if option.key in {"manifest_index", "manifest_file", "media_file"}
    )
    news_source = None
    official_image_hosts = ("webusstatic.yo-star.com", "launcher-pkg-hbr-en.yo-star.com")
