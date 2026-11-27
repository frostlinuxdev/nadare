# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Azur Promilia (JP), installed from the shared Yostar manifest feed."""

from pathlib import Path

from ..core.diskspace import human_size
from .arknights import ArknightsBackend
from .direct import DirectBackend

KIND = "azurpromilia-jp-yostar"
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/AZUPRO_JP")

#: The publisher's manifest total (every file): what a fresh install downloads and keeps.
INSTALLED_BYTES = 84_158_595_039

#: The publisher's launcher passes these start params. A profile that sets its own
#: ``launch_args`` keeps control.
LAUNCH_ARGS = ("-start=azurpromilia_launcher",)


class AzurPromiliaBackend(ArknightsBackend):
    kind = KIND
    game_tag = "AZUPRO_JP"
    display_name = "Azur Promilia (JP)"
    install_subdir = DEFAULT_INSTALL_SUBDIR
    direct_exe = "AzurPromiliaJP.exe"
    launcher_names = (direct_exe,)
    install_size_hint = INSTALLED_BYTES
    # No game-specific display or presentation workaround has been verified.
    default_display = "x11"
    dxvk_config = ""
    options = DirectBackend.options + tuple(
        option
        for option in ArknightsBackend.options
        if option.key in {"manifest_index", "manifest_file", "media_file"}
    )
    news_source = None
    official_image_hosts = ("webusstatic.yo-star.com", "launcher-pkg-azupro-jp.yo-star.com")
    page_notice = (
        f"Japanese client from Yostar. It is about {human_size(INSTALLED_BYTES)}, needs a "
        "Japanese Yostar account, and ships the publisher's anti-cheat, which is untested on "
        "Linux, so it may not start.",
        "https://azurpromilia.jp/",
        "Official site",
    )

    def launch_extra_args(self) -> list[str]:
        if self._extra_args("launch_args"):
            return []
        return list(LAUNCH_ARGS)
