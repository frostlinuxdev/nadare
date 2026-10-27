# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Games that only run through the publisher's own launcher (``launcher-<game>``).

Nadare downloads the publisher's installer, runs it in the game's Wine/Proton
prefix and then starts the installed launcher, which downloads and updates the
game itself. Everything is inherited from
:class:`~nadare.backends.arknights.ArknightsBackend`'s installer path; this module
only supplies where the installer comes from and which hosts may serve it.

Without a pinned ``installer_sha256`` the installer is only accepted from the
publisher's own hosts, and redirects must stay on them.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from ..core import graphics
from ..core.backends import BackendUnsupported, Option
from ..core.nte_launcher import INSTALLER_URL
from . import nikke_news, nte_news
from .arknights import ArknightsBackend
from .wine import profile_setting


class LauncherBackend(ArknightsBackend):
    """Base for the per-game classes below; never registered on its own."""

    kind = "launcher"
    #: Each game below sets its own; this is only a fallback for a new launcher kind.
    install_size_hint = 100 * 1024**3
    unofficial_notice = True
    news_source = None
    official_media = False
    #: No public "latest installer" API: the pinned default is used unless the
    #: profile overrides ``installer_url``.
    installer_api = None
    #: Hosts (and their subdomains) an unpinned installer may come from.
    trusted_hosts: tuple[str, ...] = ()

    options = (
        Option("prefix", "Wine prefix", "path_dir", "WINEPREFIX used for this game."),
        Option("install_dir", "Game folder", "path_dir", "Where the launcher lives."),
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
            "Wayland is the default. X11 (XWayland) can help if the window misbehaves.",
            default="auto",
            choices=(
                ("Auto (Wayland)", "auto"),
                ("Wayland", "wayland"),
                ("X11 (XWayland)", "x11"),
            ),
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

    def direct_mode(self) -> bool:
        return False

    def _installer_host_trusted(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(str(url))
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == h or host.endswith("." + h) for h in self.trusted_hosts
        )

    def login(self) -> None:
        raise BackendUnsupported(
            f"{self.display_name} logs in inside its own launcher; there is no separate login step"
        )


def _subdir(root: str, name: str) -> Path:
    return Path(root) / name


class NikkeBackend(LauncherBackend):
    kind = "launcher-nikke"
    news_source = staticmethod(nikke_news.fetch_nikke_news)
    official_image_hosts = nikke_news.IMAGE_HOSTS
    #: Measured on a real install (launcher, game and Wine prefix): 27 GiB.
    install_size_hint = 27 * 1024**3
    game_tag = "nikke"
    display_name = "NIKKE: Goddess of Victory"
    install_subdir = _subdir("drive_c", "NIKKE")
    launcher_names = ("nikke_launcher.exe",)
    direct_exe = "nikke_launcher.exe"
    default_installer_url = "https://nikke-en.com/nikkeminiloader0.0.6.346.exe"
    trusted_hosts = ("nikke-en.com", "nikke-kr.com", "jupiterlauncher.com")
    page_notice = (
        "After Nadare installs the NIKKE miniloader, click INSTALL in its window, then click "
        "INSTALL again in the installer window that opens. The launcher downloads the game. "
        "Tencent's kernel anti-cheat (ACE) may prevent it from starting under Wine, so support "
        "is experimental.",
        "https://nikke-en.com/",
        "Official site",
    )

    #: umu's default id (0) makes the Proton fixes export ``SteamAppId=0`` and the game
    #: then believes it runs on Steam and never starts its own login. A named non-Steam id
    #: avoids that; a profile ``extra_env`` GAMEID/UMU_ID still wins.
    game_id = "umu-nikke"

    def _prefix_command(
        self, args: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        argv, env = super()._prefix_command(args, extra_env)
        env["GAMEID"] = self._explicit_env("GAMEID", extra_env) or self.game_id
        env["UMU_ID"] = self._explicit_env("UMU_ID", extra_env) or env["GAMEID"]
        return argv, env

    def _explicit_env(self, key: str, extra_env: dict[str, str] | None) -> str | None:
        """A value the profile or caller set on purpose (not Nadare's own default)."""
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        for source in (extra_env, profile_env if isinstance(profile_env, dict) else None):
            if source and source.get(key):
                return str(source[key])
        return None


class NteBackend(LauncherBackend):
    kind = "launcher-nte"
    news_source = staticmethod(nte_news.fetch_nte_news)
    official_image_hosts = nte_news.IMAGE_HOSTS
    #: The launcher installs the same game as ``direct-nte``.
    install_size_hint = int(98.0 * 10**9)
    game_tag = "nte-launcher"
    display_name = "Neverness to Everness (publisher's launcher)"
    install_subdir = _subdir("drive_c/Program Files", "Neverness To Everness")
    launcher_names = ("NTEGlobalLauncher.exe",)
    direct_exe = "NTEGlobalLauncher.exe"
    default_installer_url = INSTALLER_URL
    trusted_hosts = ("perfectworld.com",)
    page_notice = (
        "The installer defaults to the S: drive, which Wine maps to your home folder. In "
        "its Custom install choose a folder on C: instead. The launcher then downloads "
        "the game (about 91 GB).",
        "https://nte.perfectworld.com/",
        "Official site",
    )


class Gfl2HaoplayBackend(LauncherBackend):
    """Girls' Frontline 2: Exilium, global build by Haoplay (the other build is ``direct-gfl2``)."""

    kind = "launcher-gfl2-haoplay"
    #: A small launcher; the game downloads about 60 GB inside itself.
    install_size_hint = 60 * 10**9
    game_tag = "gfl2-haoplay"
    display_name = "Girls' Frontline 2 (Haoplay global build, publisher's launcher)"
    install_subdir = _subdir("drive_c/Program Files", "GF2")
    launcher_names = ("PCLauncher.exe",)
    direct_exe = "PCLauncher.exe"
    default_installer_url = "https://gf2-dl.haoplay.com/pc/GF2_v1.0.3.exe"
    trusted_hosts = ("haoplay.com",)
    page_notice = (
        "This is the Haoplay global build (the Darkwinter build is a separate listing); its "
        "launcher needs the .NET and Visual C++ runtimes the installer bundles, and signs you "
        "in with an embedded web view. Choose an install folder on C: in the installer. The "
        "launcher then downloads the game (about 31 GB). On Wayland, set the profile's Display "
        "backend to Auto or Wayland if the sign-in page misbehaves.",
        "https://gf2.haoplay.com/",
        "Official site",
    )


__all__ = ["LauncherBackend"]
