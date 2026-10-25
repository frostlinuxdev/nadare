# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download games (``direct-<game>`` kinds).

These games install straight from the publisher's public patch API, with no
launcher: a per-game source (:mod:`nadare.core.sources`) turns the API into a
:class:`~nadare.core.directdl.Plan` and :mod:`nadare.core.directdl` installs it.
Everything else (Wine/Proton runner, prefix, launch, process detection, status)
is inherited from :class:`~nadare.backends.arknights.ArknightsBackend`, exactly as
Stella Sora does; only the source of the file list differs.

Profile keys are the same as ``arknights-yostar`` minus the Yostar manifest and
installer keys.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core import directdl, endfield_shim, graphics, nte_launcher
from ..core.backends import BackendError, Option
from ..core.sources import Fetch, aethergazer, bd2, endfield, gfl2, hoyo, mongil, nte, pgr, wuwa
from .arknights import INSTALLED_PREFIX_KEY, ArknightsBackend
from .nmlogin import NetmarbleLoginMixin
from .wine import profile_setting

#: Sizes below are decimal gigabytes, as the publishers and archive tables report them.
GB = 10**9


class DirectBackend(ArknightsBackend):
    """Base for the per-game classes below; never registered on its own."""

    kind = "direct"
    install_size_hint = 20 * 1024**3
    #: The community publishes these without the vendor's blessing.
    unofficial_notice = True
    news_source = None
    official_media = False
    options = (
        Option("prefix", "Wine prefix", "path_dir", "WINEPREFIX used for this game."),
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
            "Wayland is the default. X11 (XWayland) can help if the window misbehaves.",
            default="auto",
            choices=(
                ("Auto (Wayland)", "auto"),
                ("Wayland", "wayland"),
                ("X11 (XWayland)", "x11"),
            ),
        ),
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
    )

    #: ``Plan`` factory for this game: ``(fetch) -> Plan``.
    plan_factory: Callable[[Fetch | None], directdl.Plan]

    def plan_kwargs(self) -> dict[str, Any]:
        """Extra keyword arguments for ``plan_factory`` (default: none)."""
        return {}

    def plan(self) -> directdl.Plan:
        try:
            return type(self).plan_factory(None, **self.plan_kwargs())
        except directdl.DirectError as exc:
            raise BackendError(
                f"{self.display_name}: could not read the publisher's download "
                f"information ({exc}). Check your connection or try again later."
            ) from exc

    # ---- hooks the Arknights machinery calls ------------------------------
    def manifest_source(self) -> str:
        return self.game_tag

    def direct_mode(self) -> bool:
        return True

    def _require_manifest(self) -> str:
        return self.game_tag

    def _load_manifest(self) -> directdl.Plan:  # type: ignore[override]
        return self.plan()

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        plan = self.plan()
        try:
            files, archives = directdl.plan_missing(plan, self.install_dir(), verify=verify)
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        return [
            *files,
            *(SimpleNamespace(path=archive.name) for archive in archives),
            *(
                SimpleNamespace(path=pkg.name)
                for pkg in directdl.sophon_missing(plan, self.install_dir())
            ),
        ]

    def _sync_direct(self) -> dict[str, Any]:
        plan = self._load_manifest()
        workers = int(
            self.ctx.profile.get("download_workers") or directdl.plan_workers(plan)
        )
        marker = self._install_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("sync in progress\n", encoding="utf-8")
        from ..core.events import Event

        self.ctx.emit(Event("step", f"{self.display_name}: syncing {plan.version} directly"))
        try:
            summary = directdl.sync(
                plan,
                self.install_dir(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                workers=workers,
            )
        except directdl.DirectError as exc:
            raise BackendError(
                f"{self.display_name}: {exc}. Nothing was left half-installed; try again."
            ) from exc
        marker.unlink(missing_ok=True)
        self._record_synced_version(plan)
        self.info(
            f"{self.display_name}: synced {summary.get('downloaded', 0)} item(s), "
            f"{summary.get('skipped', 0)} already up to date"
        )
        return summary

    # ---- lifecycle (same shape as Stella Sora's) --------------------------
    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        self._ensure_default_runner(strict=False)
        self._ensure_direct_prefix()
        self._sync_direct()

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"{self.display_name} is not installed")
        self._sync_direct()

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._ensure_default_runner(strict=True)
        return self._launch_direct()


def _subdir(name: str) -> Path:
    return Path("drive_c/Games") / name


class BrownDust2Backend(DirectBackend):
    kind = "direct-bd2"
    game_tag = "bd2"
    display_name = "Brown Dust 2"
    install_subdir = _subdir("BrownDust2")
    direct_exe = "BrownDust II.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.4 * GB)
    plan_factory = staticmethod(bd2.resolve)


class AetherGazerBackend(DirectBackend):
    kind = "direct-aethergazer"
    game_tag = "aethergazer"
    display_name = "Aether Gazer"
    install_subdir = _subdir("AetherGazer")
    direct_exe = "AetherGazer.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(10.4 * GB)
    plan_factory = staticmethod(aethergazer.resolve)


class MongilBackend(NetmarbleLoginMixin, DirectBackend):
    kind = "direct-mongil"
    game_tag = "mongil"
    display_name = "Mongil: Star Dive"
    install_subdir = _subdir("MongilStarDive")
    direct_exe = "STARDIVE.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.6 * GB)
    plan_factory = staticmethod(mongil.resolve)
    page_notice = (
        "Mongil: Star Dive takes its login from Netmarble's launcher. Nadare does that step "
        "itself: Log in opens Netmarble's own sign-in page in your browser (no password "
        "goes through Nadare), then launch hands the game its token.",
        "https://stardive.netmarble.com/",
        "Official site",
    )


class WutheringWavesBackend(DirectBackend):
    kind = "direct-wuwa"
    game_tag = "wuwa"
    display_name = "Wuthering Waves"
    install_subdir = _subdir("WutheringWaves")
    direct_exe = "Wuthering Waves.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(85.9 * GB)
    plan_factory = staticmethod(wuwa.resolve)
    options = DirectBackend.options + (
        Option(
            "kuro_token",
            "Kuro download token",
            "text",
            "Only needed if Kuro rotates the token Nadare ships "
            "(see core/sources/wuwa.py).",
            advanced=True,
        ),
    )

    def plan_kwargs(self) -> dict[str, Any]:
        token = str(self.ctx.profile.get("kuro_token") or "").strip()
        return {"token": token} if token else {}

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

    def launch_extra_args(self) -> list[str]:
        # The launcher stub passes these; a profile that sets its own launch_args keeps control.
        if self._extra_args("launch_args"):
            return []
        return ["-dx12", "-krqlv=hd"]


class Gfl2Backend(DirectBackend):
    kind = "direct-gfl2"
    game_tag = "gfl2"
    display_name = "Girls' Frontline 2: Exilium"
    install_subdir = _subdir("GF2Exilium")
    direct_exe = "GF2_Exilium.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(88.2 * GB)
    plan_factory = staticmethod(gfl2.resolve)


class EndfieldBackend(DirectBackend):
    kind = "direct-endfield"
    game_tag = "endfield"
    display_name = "Arknights: Endfield"
    install_subdir = _subdir("ArknightsEndfield")
    direct_exe = "Endfield.exe"
    launcher_names = (direct_exe,)
    #: Streamed from the server: no archive on disk, only the 68 GB game (62 GB download).
    install_size_hint = int(68.3 * GB)
    plan_factory = staticmethod(endfield.resolve)
    page_notice = (
        "Nadare downloads Arknights: Endfield (62 GB, 68 GB installed) straight from the "
        "server. The game only starts when Hypergryph's launcher is its parent process, so "
        "Nadare starts it through a small helper (built from source with mingw-w64) that sets "
        "the launcher's Games.exe as the parent; the launcher itself never runs. Needs "
        "mingw-w64 and 7-Zip installed. Steam installs start directly.",
        "https://endfield.gryphline.com/",
        "Official site",
    )

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


class NteDirectBackend(DirectBackend):
    kind = "direct-nte"
    game_tag = "nte"
    display_name = "Neverness to Everness"
    install_subdir = _subdir("NevernessToEverness")
    direct_exe = nte.EXE
    launcher_names = (direct_exe,)
    install_size_hint = int(98.0 * GB)
    plan_factory = staticmethod(nte.resolve)
    #: The launcher's embedded Chromium (login, news) and the game's login window draw blank
    #: under native Wayland on KDE; XWayland renders them. ``display: wayland`` still wins.
    default_display = "x11"
    page_notice = (
        "Installs straight from Perfect World's servers (about 98 GB). The game's Start and "
        "login only work when Perfect World's launcher is running, so Nadare also fetches the "
        "launcher files (7-Zip needed) and starts it with /autoplay: the launcher collapses to "
        "a small bar and starts the game by itself, like Steam. The very first start may show "
        "the launcher's Update button once, to register the install.",
        "https://nte.perfectworld.com/",
        "Official site",
    )

    def _direct_argv(self, target: Path) -> list[str]:
        try:
            launcher = nte_launcher.ensure_launcher(
                self.install_dir(), data_dir=self._data_dir()
            )
        except nte_launcher.NteLauncherError as exc:
            raise BackendError(f"cannot prepare the NTE launcher: {exc}") from exc
        self.info("NTE: starting the publisher launcher (/autoplay starts the game)")
        return [str(launcher), "/launcher", "/autoplay", *super()._direct_argv(target)[1:]]


class GenshinBackend(DirectBackend):
    kind = "direct-genshin"
    game_tag = "genshin"
    display_name = "Genshin Impact"
    install_subdir = _subdir("GenshinImpact")
    direct_exe = "GenshinImpact.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(100.8 * GB)
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hk4e_global", fetch))


class PgrBackend(DirectBackend):
    kind = "direct-pgr"
    game_tag = "pgr"
    display_name = "Punishing: Gray Raven"
    install_subdir = _subdir("PunishingGrayRaven")
    direct_exe = "PGR.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(71.7 * GB)
    plan_factory = staticmethod(pgr.resolve)
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
        "Nadare downloads Punishing: Gray Raven (71.7 GB) from Kuro's CDN using the token the "
        "official launcher uses. Pointing Game folder at an existing install (Steam, Heroic) "
        "checks every file against the index and downloads only what differs.",
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
    install_subdir = _subdir("HonkaiStarRail")
    direct_exe = "StarRail.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(107.6 * GB)
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hkrpg_global", fetch))


class Hi3Backend(DirectBackend):
    kind = "direct-hi3"
    game_tag = "hi3"
    display_name = "Honkai Impact 3rd"
    install_subdir = _subdir("HonkaiImpact3rd")
    direct_exe = "BH3.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(36.7 * GB)
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("bh3_global", fetch))


class ZenlessBackend(DirectBackend):
    kind = "direct-zzz"
    game_tag = "zzz"
    display_name = "Zenless Zone Zero"
    install_subdir = _subdir("ZenlessZoneZero")
    direct_exe = "ZenlessZoneZero.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(85.9 * GB)
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("nap_global", fetch))


__all__ = ["DirectBackend"]
