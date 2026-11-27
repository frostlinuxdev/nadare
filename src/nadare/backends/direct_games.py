# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Concrete direct-download game backends; compatibility exports live in direct."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import (
    endfield_shim,
    nte_launcher,
    psoguard,
)
from ..core.backends import BackendError, Option
from ..core.diskspace import free_needed, human_size
from ..core.sources import (
    aethergazer,
    bd2,
    dna,
    endfield,
    gfl2,
    hoyo,
    mongil,
    nte,
    pgr,
    snowbreak,
)
from . import (
    bd2_news,
    endfield_news,
    gfl2_news,
    hoyo_art,
    hoyo_news,
    kuro_news,
    mongil_news,
    nte_news,
)
from .direct import GB, DirectBackend, game_subdir, publish_game_exports
from .nmlogin import NetmarbleLoginMixin
from .webview2_signin import WebView2SignInMixin


class BrownDust2Backend(DirectBackend):
    kind = "direct-bd2"
    news_source = staticmethod(bd2_news.fetch_bd2_news)
    official_image_hosts = bd2_news.IMAGE_HOSTS
    game_tag = "bd2"
    display_name = "Brown Dust 2"
    install_subdir = game_subdir("BrownDust2")
    direct_exe = "BrownDust II.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.4 * GB)
    plan_factory = staticmethod(bd2.resolve)


class AetherGazerBackend(DirectBackend):
    kind = "direct-aethergazer"
    game_tag = "aethergazer"
    display_name = "Aether Gazer"
    install_subdir = game_subdir("AetherGazer")
    direct_exe = "AetherGazer.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(10.4 * GB)
    plan_factory = staticmethod(aethergazer.resolve)


DNA_ACCOUNT_NOTICE = """\
BEFORE YOU PLAY: DUET NIGHT ABYSS

Nadare is unofficial and not supported by Pan Studio or Hero Games. Using it may be against
the game's terms of service, and your account could be restricted. Do not use an account you
cannot afford to lose.

The game uses Anti-Cheat Expert (ACE), a kernel-level anti-cheat on Windows. On Linux it runs
under Wine/Proton like the rest of the game; whether the publisher treats that as allowed is
their decision, not Nadare's.

Nadare installs the game straight from the publisher's servers and never runs the publisher's
launcher: on 2026-03-18 an update of that launcher shipped malware, which Pan Studio confirmed.
For the same reason this game always runs in the Sandbox (your home folder stays hidden from it),
and it does not start at all where the Sandbox cannot run.
"""


#: The fresh install's diff, which unpacks to the installed tree below.
DNA_DOWNLOAD_BYTES = int(28.4 * GB)
#: The finished install.
DNA_INSTALLED_BYTES = int(31 * GB)
#: Peak free space: the diff stays on disk until its files are unpacked.
DNA_PEAK_BYTES = int(60.0 * GB)


class DnaBackend(DirectBackend):
    kind = "direct-dna"
    game_tag = "dna"
    display_name = "Duet Night Abyss"
    install_subdir = game_subdir("DuetNightAbyss")
    direct_exe = dna.EXE
    launcher_names = (direct_exe, dna.GAME_EXE)
    #: The client the stub hands off to; the stub itself exits. For the launch sheet
    #: only: ``game_process_names`` also drives Flatpak running detection.
    first_frame_process_names = (dna.GAME_EXE.rsplit("/", 1)[-1],)
    install_size_hint = DNA_INSTALLED_BYTES
    download_size_hint = DNA_DOWNLOAD_BYTES
    #: The peak: the diff stays on disk until its files are unpacked
    #: (``diskspace.install_space`` refuses a fresh install below this).
    install_space_hint = DNA_PEAK_BYTES
    plan_factory = staticmethod(dna.resolve)
    sandbox_required = True
    #: DW-Proton's fix for DNA (gamefixes-umu/umu-3950020.py -> gamefixes-steam/3950020.py)
    #: disables dcomp so the game's CEF pages draw, and sets PROTON_ENABLE_INT3_HACK; as
    #: umu-default it never loaded and the window stayed white (2026-11-17, an NVIDIA PC).
    umu_id = "umu-3950020"
    #: On native Wayland the window stays white (DXVK and wined3d alike); on X11 the same
    #: prefix, runner and Sandbox reach the game (#1608, an AMD PC, 2026-11-18).
    default_display = "x11"
    #: From ``Z:\\home\\...`` (Add Game's ``<library>/<Game>/install``) the sign-in program
    #: crashes on its first run and the game stays black; as ``D:`` it starts (#1608, an AMD PC).
    game_drive_letter = "d"
    account_notice = DNA_ACCOUNT_NOTICE
    page_notice = (
        "Installs straight from Pan Studio's servers: one "
        f"{human_size(DNA_DOWNLOAD_BYTES)} download that unpacks to about "
        f"{human_size(DNA_INSTALLED_BYTES)} (both are on disk for a while, so keep about "
        f"{human_size(free_needed(DNA_PEAK_BYTES))} free). The publisher's launcher "
        "never runs, and the game always runs in the Sandbox.",
        "https://duetnightabyss.dna-panstudio.com/",
        "Official site",
    )


#: The installed client (the Store's figure and what a fresh install fetches).
SNOWBREAK_BYTES = int(54.0 * GB)


class SnowbreakBackend(DirectBackend):
    kind = "direct-snowbreak"
    game_tag = "snowbreak"
    display_name = "Snowbreak: Containment Zone"
    #: Its sign-in is a full-screen layered window: black and off-centre on native Wayland,
    #: centred and transparent on X11 (an AMD PC, KDE, 2026-11-21).
    default_display = "x11"
    install_subdir = game_subdir("Snowbreak")
    direct_exe = snowbreak.EXE
    launcher_names = (direct_exe,)
    install_size_hint = SNOWBREAK_BYTES
    plan_factory = staticmethod(snowbreak.resolve)
    #: What Seasun's own launcher passes (3.6.0: ``-FeatureLevelES31 -ChannelID=%s``). The PC
    #: client renders with UE's ES3.1 feature level and ships only those global shaders: without
    #: the flag it stops at "Game files required to initialize the global shader library are
    #: missing" (seen on an AMD PC, 2026-11-16). The channel picks the account SDK. A profile
    #: that sets its own launch_args keeps control.
    snowbreak_args: tuple[str, ...] = ("-FeatureLevelES31", "-ChannelID=seasun")
    page_notice = (
        f"Installs straight from Seasun's servers (about {human_size(SNOWBREAK_BYTES)}), file "
        "by file, with every file checked against Seasun's list. You sign in inside the game.",
        "https://snowbreak.amazingseasun.com/",
        "Official site",
    )

    def launch_extra_args(self) -> list[str]:
        if self._extra_args("launch_args"):
            return []
        return list(self.snowbreak_args)


class MongilBackend(NetmarbleLoginMixin, DirectBackend):
    kind = "direct-mongil"
    news_source = staticmethod(mongil_news.fetch_mongil_news)
    official_image_hosts = mongil_news.IMAGE_HOSTS
    vcredist_default = True
    game_tag = "mongil"
    display_name = "Mongil: Star Dive"
    install_subdir = game_subdir("MongilStarDive")
    direct_exe = "STARDIVE.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.6 * GB)
    plan_factory = staticmethod(mongil.resolve)
    page_summary = (
        "Log in opens Netmarble's sign-in page in your browser (no password goes through "
        "Nadare); Play then hands the game its token."
    )
    page_notice = (
        "Mongil: Star Dive takes its login from Netmarble's launcher. Nadare does that step "
        "itself: Log in opens Netmarble's own sign-in page in your browser (no password "
        "goes through Nadare), then launch hands the game its token.",
        "https://stardive.netmarble.com/",
        "Official site",
    )


class Gfl2Backend(DirectBackend):
    kind = "direct-gfl2"
    news_source = staticmethod(gfl2_news.fetch_gfl2_news)
    official_image_hosts = gfl2_news.IMAGE_HOSTS
    game_tag = "gfl2"
    display_name = "Girls' Frontline 2: Exilium (Darkwinter build)"
    install_subdir = game_subdir("GF2Exilium")
    direct_exe = "GF2_Exilium.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(88.2 * GB)
    plan_factory = staticmethod(gfl2.resolve)
    page_notice = (
        "This is the Darkwinter build, downloaded from Sunborn's CDN. Girls' Frontline 2 also "
        "has a separate Haoplay global build, listed on its own; the two are different "
        "installs, so pick the one you play.",
        "https://store.steampowered.com/app/3347400/",
        "Steam page",
    )


#: The client plus its game data, with one voice language.
GFL2_HAOPLAY_BYTES = int(94.0 * GB)


class Gfl2HaoplayBackend(WebView2SignInMixin, Gfl2Backend):
    """The Haoplay global build: same client layout, downloaded from Haoplay's CDN."""

    kind = "direct-gfl2-haoplay"
    # Sunborn's feed is the Darkwinter servers' (its maintenance times may not match);
    # Haoplay's site has only fixed banner images, no feed (#535, checked 2026-11-08).
    news_source = None
    official_image_hosts = DirectBackend.official_image_hosts
    game_tag = "gfl2-haoplay"
    display_name = "Girls' Frontline 2 (Haoplay global build)"
    install_subdir = game_subdir("GF2Haoplay")
    install_size_hint = GFL2_HAOPLAY_BYTES  # the client plus ~93.7 GB of game data
    plan_factory = staticmethod(gfl2.resolve_haoplay)
    #: The game only starts under umu's id 0 (``wine start /wait /unix``); a named id or a Steam
    #: app id makes Proton run it through steam.exe and it hangs. A profile ``extra_env`` wins.
    umu_id = "0"

    page_summary = (
        f"Nadare downloads the game files (about {human_size(GFL2_HAOPLAY_BYTES)}) from "
        "Haoplay's servers. If the sign-in page misbehaves, try the other Display backend in "
        "Settings."
    )
    page_notice = (
        "This is the Haoplay global build; the Darkwinter build is a separate listing. Nadare "
        "downloads the game files and the Haoplay client straight from Haoplay's servers "
        f"(no launcher), about {human_size(GFL2_HAOPLAY_BYTES)} with one voice language (more "
        "with others). Nadare installs Microsoft's WebView2 Runtime into this game's prefix for "
        "sign-in. If the sign-in page misbehaves, try the other Display backend in Settings.",
        "https://gf2.haoplay.com/",
        "Official site",
    )


#: Streamed from the server: no archive on disk, only the installed game.
ENDFIELD_DOWNLOAD_BYTES = int(62 * GB)
ENDFIELD_INSTALLED_BYTES = int(68.3 * GB)


class EndfieldBackend(DirectBackend):
    kind = "direct-endfield"
    news_source = staticmethod(endfield_news.fetch_endfield_news)
    official_image_hosts = endfield_news.IMAGE_HOSTS
    game_tag = "endfield"
    display_name = "Arknights: Endfield"
    install_subdir = game_subdir("ArknightsEndfield")
    direct_exe = "Endfield.exe"
    launcher_names = (direct_exe,)
    install_size_hint = ENDFIELD_INSTALLED_BYTES
    download_size_hint = ENDFIELD_DOWNLOAD_BYTES
    plan_factory = staticmethod(endfield.resolve)
    page_notice = (
        "Nadare downloads Arknights: Endfield "
        f"({human_size(ENDFIELD_DOWNLOAD_BYTES)} download, "
        f"{human_size(ENDFIELD_INSTALLED_BYTES)} installed) straight from the "
        "server. The game only starts when Hypergryph's launcher is its parent process, so "
        "Nadare starts it through a small bundled helper (source in the repository, built with "
        "mingw-w64) that sets the launcher's Games.exe as the parent; the launcher itself "
        "never runs. Steam installs start directly.",
        "https://endfield.gryphline.com/",
        "Official site",
    )

    def launch(self) -> int:
        """Play, with the shader cache kept from a cut-short save (#1421)."""
        prefix = self.prefix()
        guard = psoguard.Guard(
            psoguard.endfield_cache(prefix),
            self._data_dir() / "pso-backup",
            self.info,
            home=psoguard.wine_user_dir(prefix),
        )
        guard.before()
        marker = getattr(self, "_launch_started", None)
        try:
            return super().launch()
        finally:
            # Only after a game this call started: when Play was refused (already running in
            # another Nadare), that session may still be writing the file.
            if getattr(self, "_launch_started", None) is not marker:
                guard.after()

    def _direct_argv(self, target: Path) -> list[str]:
        argv = super()._direct_argv(target)
        if endfield_shim.is_steam_build(target.parent):
            return argv
        prefix = self.prefix()
        try:
            files = endfield_shim.ensure_files(prefix, data_dir=self._data_dir())
        except endfield_shim.ShimError as exc:
            raise BackendError(f"cannot prepare the Endfield launch helper: {exc}") from exc
        self.info("Endfield: starting through the launcher helper (Games.exe as parent)")
        return endfield_shim.launch_argv(
            str(files.shim),
            endfield_shim.win_path(prefix, files.games),
            endfield_shim.win_path(prefix, target),
            argv[1:],
        )


#: The game files, then the launcher's smaller packs.
NTE_GAME_BYTES = int(98.0 * GB)
NTE_PACKS_BYTES = int(2.3 * GB)


class NteDirectBackend(DirectBackend):
    game_process_names = ("HTGame.exe",)
    kind = "direct-nte"
    news_source = staticmethod(nte_news.fetch_nte_news)
    official_image_hosts = nte_news.IMAGE_HOSTS
    game_tag = "nte"
    display_name = "Neverness to Everness"
    install_subdir = game_subdir("NevernessToEverness")
    direct_exe = nte.EXE
    launcher_names = (direct_exe,)
    install_size_hint = NTE_GAME_BYTES
    plan_factory = staticmethod(nte.resolve)
    #: The launcher's embedded Chromium (login, news) and the game's login window draw blank
    #: under native Wayland on KDE; XWayland renders them. ``display: wayland`` still wins.
    default_display = "x11"
    page_summary = (
        "Perfect World's launcher starts with the game as a small bar and starts it by "
        "itself; that is expected."
    )
    page_notice = (
        "Installs straight from Perfect World's servers "
        f"(about {human_size(NTE_GAME_BYTES)}), in two passes: the game files first, then 7 "
        f"smaller packs (about {human_size(NTE_PACKS_BYTES)}). The game's Start and login only "
        "work when Perfect World's launcher is running, so Nadare also fetches the launcher files "
        "(7-Zip needed) and starts it with /autoplay: the launcher collapses to a small bar and "
        "starts the game by itself, like Steam. On the very first start the launcher updates "
        "itself once (a minute or two) and may show its Update button once, to register the "
        "install.",
        "https://nte.perfectworld.com/",
        "Official site",
    )

    def _direct_argv(self, target: Path) -> list[str]:
        try:
            launcher = nte_launcher.ensure_launcher(self.install_dir(), data_dir=self._data_dir())
        except nte_launcher.NteLauncherError as exc:
            raise BackendError(f"cannot prepare the NTE launcher: {exc}") from exc
        self.info("NTE: starting the publisher launcher (/autoplay starts the game)")
        return [str(launcher), "/launcher", "/autoplay", *super()._direct_argv(target)[1:]]


class GenshinBackend(DirectBackend):
    kind = "direct-genshin"
    game_tag = "genshin"
    display_name = "Genshin Impact"
    install_subdir = game_subdir("GenshinImpact")
    direct_exe = "GenshinImpact.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(100.8 * GB)
    news_source = staticmethod(hoyo_news.fetch_genshin_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_genshin_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hk4e_global", fetch))
    supports_predownload = True
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("hk4e_global", fetch)
    )


#: The installed client (the Store's figure and what a fresh install fetches).
PGR_BYTES = int(71.7 * GB)


class PgrBackend(DirectBackend):
    kind = "direct-pgr"
    news_source = staticmethod(kuro_news.fetch_pgr_news)
    official_image_hosts = kuro_news.IMAGE_HOSTS
    game_tag = "pgr"
    display_name = "Punishing: Gray Raven"
    tidies_retired = True
    install_subdir = game_subdir("PunishingGrayRaven")
    direct_exe = "PGR.exe"
    launcher_names = (direct_exe,)
    install_size_hint = PGR_BYTES
    plan_factory = staticmethod(pgr.resolve)
    supports_predownload = True
    predownload_factory = staticmethod(pgr.resolve_predownload)
    #: Native Wayland gave a white window in testing; the X11 path reached the game's own
    #: agreement screen. Not yet re-confirmed through Nadare with display set to x11.
    default_display = "x11"
    options = DirectBackend.options + (
        Option(
            "kuro_token",
            "Kuro download token",
            "text",
            "Only needed if Kuro rotates the token Nadare ships (see core/sources/pgr.py).",
            advanced=True,
        ),
    )
    page_notice = (
        f"Nadare downloads Punishing: Gray Raven ({human_size(PGR_BYTES)}) from Kuro's CDN "
        "using the token the official launcher uses. Pointing Game folder at an existing "
        "install (Steam, Heroic) checks every file against the index and downloads only what "
        "differs.",
        "https://pgr.kurogame.net/",
        "Official site",
    )

    def plan_kwargs(self) -> dict[str, Any]:
        token = str(self.ctx.profile.get("kuro_token") or "").strip()
        return {"token": token} if token else {}


class StarRailBackend(DirectBackend):
    kind = "direct-hsr"
    game_tag = "hsr"
    display_name = "Honkai: Star Rail"
    install_subdir = game_subdir("HonkaiStarRail")
    direct_exe = "StarRail.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(107.6 * GB)
    news_source = staticmethod(hoyo_news.fetch_hsr_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_hsr_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hkrpg_global", fetch))
    supports_predownload = True
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("hkrpg_global", fetch)
    )


class Hi3Backend(DirectBackend):
    kind = "direct-hi3"
    game_tag = "hi3"
    display_name = "Honkai Impact 3rd"
    install_subdir = game_subdir("HonkaiImpact3rd")
    direct_exe = "BH3.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(36.7 * GB)
    news_source = staticmethod(hoyo_news.fetch_hi3_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_hi3_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("bh3_global", fetch))
    supports_predownload = True
    predownload_factory = staticmethod(
        lambda fetch=None, **kw: hoyo.resolve_predownload("bh3_global", fetch)
    )


class ZenlessBackend(DirectBackend):
    kind = "direct-zzz"
    game_tag = "zzz"
    display_name = "Zenless Zone Zero"
    install_subdir = game_subdir("ZenlessZoneZero")
    direct_exe = "ZenlessZoneZero.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(85.9 * GB)
    news_source = staticmethod(hoyo_news.fetch_zzz_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_zzz_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("nap_global", fetch))
    supports_predownload = True
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("nap_global", fetch)
    )


publish_game_exports(__name__, globals())
