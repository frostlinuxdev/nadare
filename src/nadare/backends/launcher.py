# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Games that only run through the publisher's own launcher (``launcher-<game>``).

Nadare downloads the publisher's installer, runs it in the game's Wine/Proton
prefix and then starts the installed launcher, which downloads and updates the
game itself. Everything is inherited from
:class:`~nadare.backends.winegame.WineGameBackend`'s installer path; this module
only supplies where the installer comes from and which hosts may serve it.

Without a pinned ``installer_sha256`` the installer is only accepted from the
publisher's own hosts, and redirects must stay on them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ..core import (
    endfield_shim,
    graphics,
    monitors,
    paths,
    process,
    railrepo,
    runtimewatch,
    sevenzip,
    trayidle,
    x11windows,
)
from ..core.backends import PREFIX_HELP, BackendError, BackendUnsupported, Option, Status
from ..core.config import as_bool, valid_profile_name
from ..core.events import Cancelled, Event
from ..core.process import process_age_seconds
from ..core.sources import nikke as nikke_source
from ..core.trayidle import HiddenWindowWatch
from . import arknights as _arknights
from . import nikke as nikke_sail
from . import nikke_news
from .nikke import session as nikke_session
from .wine import ProcessQuery, profile_setting, scan_pids, stream
from .winegame import UPDATE_CHECK_INTERVAL, WineGameBackend

log = logging.getLogger(__name__)

#: The silent downloader still opens its window. This small Windows program (source in
#: ``core/shim_src/quiet_start.c``, built with mingw-w64) starts it hidden and keeps hiding the
#: windows it opens until it exits. It is used only when it matches the pins; a test keeps the
#: pins in step with the source.
QUIET_NAME = "nadare_quiet_start.exe"
QUIET_PREBUILT = Path(__file__).resolve().parents[1] / "assets" / "nikke_quiet_start.exe"
QUIET_PREBUILT_SHA256 = "2574d409d3974368a10c14851664fa65207fb4d709edf822ca0891a8697b5e4e"
QUIET_SOURCE_SHA256 = "f5224e12ed14fe977e96b94e0182af3867b3c8307eac1fec0354bcaac49888e0"


def _stage_quiet_helper(folder: Path) -> Path | None:
    """Copy the pinned helper into ``folder``; ``None`` when it is missing or does not match."""
    try:
        data = QUIET_PREBUILT.read_bytes()
        if hashlib.sha256(data).hexdigest() != QUIET_PREBUILT_SHA256:
            return None
        target = folder / QUIET_NAME
        target.write_bytes(data)
    except OSError:
        return None
    return target


#: About what the silent NIKKE launcher install writes (#1000): the scale for its progress bar.
_SILENT_LAUNCHER_BYTES = 700 * 10**6
#: How often the launcher folder is measured while the hidden downloader runs.
_SILENT_POLL_SECONDS = 2.0


def _folder_bytes(root: Path, limit: int = 50_000) -> int:
    """Bytes of the plain files under ``root``; links are not followed, ``limit`` entries max."""
    total = seen = 0
    pending = [root]
    while pending and seen < limit:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _file_key(path: Path) -> tuple[int, int] | None:
    """Size and mtime of ``path``; ``None`` when it does not exist."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _non_empty(path: Path) -> bool:
    """``path`` is a file with something in it (a killed sign-in leaves an empty one)."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _newer(path: Path, than: Path) -> bool:
    """``path`` exists and was written after ``than`` (or ``than`` is missing)."""
    try:
        mine = path.stat().st_mtime_ns
    except OSError:
        return False
    try:
        return mine > than.stat().st_mtime_ns
    except OSError:
        return True


class SilentInstallUnavailable(Exception):
    """The silent install cannot be used here; the installer window is the way instead."""


@dataclasses.dataclass(frozen=True)
class _MissingClientFile:
    """A launcherless game's own file that Verify/Repair report as missing or damaged."""

    path: str


#: Unpacking a 20 MB installer takes seconds; anything past this is stuck.
EXTRACT_TIMEOUT = 120


def _extract_nsis(
    sevenz: str, installer: Path, outdir: Path, run: Callable[..., Any] = subprocess.run
) -> None:
    """Unpack an NSIS installer with 7-Zip (the miniloader is an NSIS wrapper)."""
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        result = run(
            [sevenz, "x", "-tNsis", "-y", f"-o{outdir}", str(installer)],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SilentInstallUnavailable(f"unpacking {installer.name} took too long") from exc
    except OSError as exc:
        raise SilentInstallUnavailable(f"cannot unpack {installer.name}: {exc}") from exc
    if result.returncode != 0:
        raise SilentInstallUnavailable(
            f"cannot unpack {installer.name} with {sevenz}: {str(result.stderr).strip()}"
        )


def _find_inner_loader(folder: Path) -> Path:
    """The downloader inside an unpacked miniloader (its name has changed between versions)."""
    try:
        found = next(
            (
                p
                for p in sorted(folder.glob("*.exe"))
                if p.is_file() and "miniloader" in p.name.lower()
            ),
            None,
        )
    except OSError:
        found = None
    if found is None:
        raise SilentInstallUnavailable("the installer does not contain the downloader")
    return found


def _wine_user_dir(prefix: Path) -> Path:
    users = prefix / "drive_c" / "users"
    proton = users / "steamuser"
    if proton.is_dir() and not proton.is_symlink():
        return proton
    try:
        other = next(
            p
            for p in sorted(users.iterdir())
            if p.is_dir() and not p.is_symlink() and p.name != "Public"
        )
    except (OSError, StopIteration):
        return prefix / "drive_c" / "nadare"
    return other


class LauncherBackend(WineGameBackend):
    """Base for the per-game classes below; never registered on its own."""

    kind = "launcher"
    supports_secure_mode = True
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
        Option("prefix", "Wine prefix", "path_dir", PREFIX_HELP),
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
        Option(
            "secure_mode",
            "Sandbox",
            "bool",
            "Hides your home folder from the launcher and the game (needs bubblewrap). They can "
            "still use the network. The launcher may fail to update itself. See "
            "docs/SECURE-MODE.md.",
            default=True,
        ),
        Option("wine_binary", "Wine binary", "path_file", advanced=True),
        Option("umu_binary", "umu-run binary", "path_file", advanced=True),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        monitors.OPTION,
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


#: NIKKE programs that are only the tray launcher and its helpers. Anything else in the prefix
#: (the game, updaters, crash handlers) counts as busy and keeps the launcher alive.
_NIKKE_IDLE_EXES = frozenset(
    {
        "nikke_launcher.exe",
        "tbs_browser.exe",
        "intl_service.exe",
        "intlwebviewhelper.exe",
        "wineserver.exe",
        "services.exe",
        "winedevice.exe",
        "plugplay.exe",
        "svchost.exe",
        "rpcss.exe",
        "explorer.exe",
        "csrss.exe",
        "lsass.exe",
        "tabtip.exe",
        "xalia.exe",
        "conhost.exe",
        "start.exe",
        "umu.exe",
        "wineboot.exe",
    }
)


def _exe_basename(value: str) -> str:
    """The lower-cased file name of a Windows or Unix path."""
    return str(value).replace("\\", "/").rsplit("/", 1)[-1].lower()


def nikke_busy(argv0s: Iterable[str]) -> bool:
    """Whether any ``.exe`` in the prefix is more than the tray launcher and Wine itself."""
    for argv0 in argv0s:
        name = _exe_basename(argv0)
        if name.endswith(".exe") and name not in _NIKKE_IDLE_EXES:
            return True
    return False


def _x11_by_default(options: tuple[Option, ...]) -> tuple[Option, ...]:
    """``options`` with the Display backend saying that "auto" means X11 for this game."""
    out = []
    for option in options:
        if option.key == "display":
            choices = tuple(
                ("Auto (X11 with the launcher)" if value == "auto" else text, value)
                for text, value in option.choices
            )
            option = dataclasses.replace(
                option,
                help=(
                    "Auto runs the NIKKE launcher and its sign-in on X11 (XWayland): they do not "
                    "work on native Wayland. When the game starts without the launcher, Auto "
                    "runs the game itself on Wayland. Pick X11 or Wayland to use one for all."
                ),
                choices=choices,
            )
        out.append(option)
    return tuple(out)


#: A NIKKE launcher this old with no window was closed to the tray (it opens in a few seconds).
TRAY_HINT_AFTER_SECONDS = 20.0


class NikkeBackend(LauncherBackend):
    kind = "launcher-nikke"
    supports_login = True
    news_source = staticmethod(nikke_news.fetch_nikke_news)
    official_image_hosts = nikke_news.IMAGE_HOSTS
    #: Measured on a real install (launcher, game and Wine prefix): 27 GiB.
    install_size_hint = 27 * 1024**3
    game_tag = "nikke"
    display_name = "NIKKE: Goddess of Victory"
    install_subdir = _subdir("drive_c", "NIKKE")
    launcher_names = ("nikke_launcher.exe",)
    direct_exe = "nikke_launcher.exe"
    default_installer_url = nikke_source.MINILOADER_URL
    installer_pin_tag = "nikke-installer"
    trusted_hosts = ("nikke-en.com", "nikke-kr.com", "jupiterlauncher.com")
    page_summary = (
        "Play opens NIKKE's own launcher: sign in there and it downloads the game's resources. "
        "When you close it, choose Exit launcher; Minimize keeps it in the tray (then press the "
        "x next to PLAYING)."
    )
    #: Under native Wayland the launcher's embedded Chromium (tbs_browser.exe) hung its GPU process
    #: and stayed blank white (9 of 9 runs on a KDE Wayland desktop, #1124); on X11 it drew in 10 s.
    #: ``display: wayland`` still wins.
    default_display = "x11"
    # No install folder: NIKKE's launcher installs inside the prefix (C:\\NIKKE). A folder set
    # outside it was ignored by the game but opened to it by the Sandbox (2026-11-11).
    options = _x11_by_default(
        tuple(o for o in LauncherBackend.options if o.key != "install_dir")
    ) + (
        Option(
            "launcherless",
            "Start the game without the NIKKE launcher",
            "bool",
            "Opens the game directly instead of the launcher: no launcher window and no captcha. "
            "It needs the game already installed and one sign-in done in the launcher. If the "
            "saved sign-in stops working, turn this off and press Play to sign in again.",
            default=False,
        ),
        Option(
            "stop_when_hidden",
            "Stop the launcher when its window is closed",
            "bool",
            "Closing the NIKKE launcher with Minimize (its default) keeps it running in the "
            "tray. With this on, Nadare stops it about a minute after its window is closed, "
            "unless the game or an update is running. Works with the X11 display backend (the "
            "default for NIKKE).",
            default=False,
        ),
    )
    page_notice = (
        "Nadare installs the NIKKE launcher and the game program for you, without opening any "
        "window (about 1.5 GB). Press Play to open the launcher and sign in there: it downloads "
        "the rest of the game (about 23 GB). Or press Log in with Google and turn on \"Start "
        "the game without the NIKKE launcher\" in Settings. Closing the launcher: pick Exit "
        "launcher and tick Save selection; its default, Minimize, keeps it in the tray (press "
        "the x next to PLAYING to end it). If the silent install fails, Nadare opens the NIKKE "
        "miniloader: click INSTALL (also in a second installer window, if one opens), then "
        "LAUNCH. Tencent's anti-cheat (ACE) may stop the game "
        "after an update. NIKKE uses X11 by default: on native Wayland its sign-in and captcha "
        "windows can stay blank (Display backend in Settings changes it).",
        "https://nikke-en.com/",
        "Official site",
    )

    #: umu's default id (0) makes the Proton fixes export ``SteamAppId=0`` and the game
    #: then believes it runs on Steam and never starts its own login. A named non-Steam id
    #: avoids that; a profile ``extra_env`` GAMEID/UMU_ID still wins.
    game_id = "umu-nikke"

    #: What the miniloader's own downloader accepts: it needs no window with /Silent, installs
    #: to /Dst=, and only the literal value ``false`` of /StartAfterInstall= stops it from
    #: starting the launcher afterwards.
    STEAM_VARIABLES = ("SteamGameId", "SteamAppId", "UMU_USE_STEAM")
    SILENT_SWITCHES = ("/Silent", "/StartAfterInstall=false")

    def _silent_wanted(self) -> bool:
        if os.environ.get("NADARE_NIKKE_SILENT") == "0":
            return False
        # Arguments the user chose for the installer mean "run it my way".
        return not self._extra_args("installer_args")

    def _direct_wanted(self) -> bool:
        if os.environ.get("NADARE_NIKKE_DIRECT") == "0":
            return False
        return not self._extra_args("installer_args")

    def _hidden_watch(self) -> trayidle.HiddenWindowWatch | None:
        """The tray watch when the option is on and NIKKE really runs on X11."""
        if not as_bool(profile_setting(self.ctx.profile, "stop_when_hidden"), False):
            return None
        if not self._display_in_use().startswith("x11"):
            return None
        return HiddenWindowWatch(
            shown=lambda: x11windows.any_shown(("NIKKE",)),
            busy=self._nikke_busy_now,
            on_idle=self._stop_hidden_launcher,
        )

    def _nikke_busy_now(self) -> bool:
        """Whether anything besides the tray launcher runs in this profile's prefix."""
        argv0s = []
        for pid in scan_pids(ProcessQuery(prefix=str(self.prefix()))):
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            head = raw.split(b"\0", 1)[0]
            if head:
                argv0s.append(head.decode("utf-8", "replace"))
        return nikke_busy(argv0s)

    def _stop_hidden_launcher(self) -> None:
        """The window stayed in the tray: stop the launcher and end the launch."""
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping the hidden NIKKE launcher: {pids}")
            self._stop_game_processes(pids)
        self.ctx.cancel.cancel()

    def launch(self) -> int:
        if self._launcherless_wanted():
            if self._client_missing_but_signed_in():
                # A launcher-only install (b22 left the client to the launcher): fetch the client
                # the way Install does now, under the same guards as an update before Play.
                self._update_before_play()
            files = self._launcherless_plan()
            if files is not None:
                # Only now: a Play that falls back to the launcher lets the launcher update.
                self._update_before_play()
                # The normal Play path sets up the pinned runner; this one has to as well, or a
                # newly pinned DW-Proton is missing and the helper ran on a plain Wine (#1338).
                self._ensure_default_runner(strict=True)
                return self._launch_launcherless(files)
        return self._launch_with_watch()

    def _launch_with_watch(self) -> int:
        watch = self._hidden_watch()
        if watch is None:
            return super().launch()
        watch.start()
        try:
            return super().launch()
        except Cancelled:
            if watch.fired:
                self.info("the NIKKE launcher window was closed; stopped the launcher")
                return 0
            raise
        finally:
            watch.stop()

    # ------------------------------------------------------------- client updates
    # Without the launcher Nadare keeps the game client current itself: the publisher's anonymous
    # GetVersion API names the current client and the CRC32 of nikke.exe, and install_game over
    # an install fetches only the files whose size/MD5 differ (nothing is run; the update's
    # install_script is empty). With the launcher, the launcher updates the game as before.

    def _client_exe(self) -> Path:
        return self._launcher_root() / "NIKKE" / "game" / "nikke.exe"

    def _update_marker(self) -> Path:
        """Present while a direct client update is unfinished (nikke.exe alone cannot tell:
        it may already be new when a later file failed)."""
        return self._launcher_root() / ".nadare-update-incomplete"

    def _client_latest_path(self) -> Path:
        return paths.cache_dir() / "nikke-latest" / f"{self.game_tag}.json"

    def _read_client_latest(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._client_latest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("md5"), str) or not isinstance(payload.get("size"), int):
            return None
        return payload

    def _fetch_client_latest(self) -> dict[str, Any]:
        """Ask the publisher for the current client and cache it; raises on failure."""
        info = nikke_source.client_info()
        entry = nikke_source.client_exe_entry(info)
        name = str(info.get("version_name") or "")
        latest = {
            "size": int(entry.size),
            "md5": bytes(entry.md5).hex(),
            "version": name.rsplit("_", 1)[-1] if name else str(info.get("version_id") or ""),
            "version_name": name,
            "checked_at": int(time.time()),
        }
        try:
            path = self._client_latest_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(latest), encoding="utf-8")
        except OSError as exc:
            log.debug("NIKKE: could not cache the client version: %s", exc)
        return latest

    def _client_outdated(self, latest: dict[str, Any] | None) -> bool:
        """Whether nikke.exe differs from the published one (size first, then MD5)."""
        if self._update_marker().exists():
            return True
        if latest is None:
            return False
        exe = self._client_exe()
        try:
            if exe.stat().st_size != latest["size"]:
                return True
            digest = hashlib.md5()
            with open(exe, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
        except OSError:
            return False  # not installed: Install, not Update
        return digest.hexdigest() != latest["md5"]

    def _managed_client_ok(self) -> bool:
        """Whether the Nadare-managed launcherless client is present and matches the publisher."""
        return self._client_exe().is_file() and not self._client_outdated(
            self._read_client_latest() or None
        )

    def _missing_entries(self, *, verify: bool = True) -> list[_MissingClientFile]:
        """The client file Repair must fetch when the launcherless client is damaged."""
        del verify
        if self._managed_client_ok():
            return []
        return [_MissingClientFile("NIKKE/game/nikke.exe")]

    def _sync_direct(self) -> dict[str, Any]:
        """Repair a launcherless client by downloading what differs from the publisher."""
        self._update_client_direct()
        return {}

    def verify(self) -> dict[str, Any]:
        report = super().verify()
        if not self._launcherless_wanted():
            return report
        ok = self._managed_client_ok()
        report["checks"]["client"] = ok
        if not ok:
            report["verified"] = False
            failed = list(report.get("failed") or ())
            if "client" not in failed:
                failed.append("client")
            report["failed"] = failed
            report["message"] = (
                "integrity check failed: the NIKKE game client is missing, damaged or "
                "outdated; click Repair files or Update"
            )
        return report

    def repair(self) -> None:
        if not self._launcherless_wanted():
            return super().repair()
        if not self._client_exe().is_file():
            raise BackendError("NIKKE is not installed yet")
        self.ctx.emit(Event("step", "Checking the NIKKE game client"))
        self._sync_direct()
        try:
            missing = self._missing_entries(verify=False)
        except (railrepo.RailError, OSError) as exc:
            raise BackendError(f"could not check the NIKKE game client ({exc})") from exc
        if missing:
            names = ", ".join(entry.path for entry in missing[:5])
            raise BackendError(
                f"{len(missing)} game file(s) are still damaged: {names}. "
                "Try again, or reinstall the game."
            )
        self.ctx.emit(Event("done", "All game files check out"))

    def check_update(self) -> None:
        """Refresh the cached client version (launcherless only, offline-safe, every few hours)."""
        if not self._launcherless_wanted():
            return
        if not self.ctx.config.data.get("check_updates", True):
            return
        cached = self._read_client_latest()
        if cached is not None:
            try:
                age = time.time() - float(cached.get("checked_at", 0))
            except (TypeError, ValueError):
                age = UPDATE_CHECK_INTERVAL
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
            self._fetch_client_latest()
        except (railrepo.RailError, OSError, ValueError) as exc:
            log.debug("NIKKE: update check failed: %s", exc)
        if self._launcher_dir() is not None:
            try:
                self._fetch_launcher_latest()
            except (railrepo.RailError, OSError, ValueError) as exc:
                log.debug("NIKKE: launcher check failed: %s", exc)

    def _client_update_status(self, result: Status) -> None:
        """Purely local: compare the client and launcher with their cached published records."""
        if not self._launcherless_wanted():
            return
        client = self._read_client_latest()
        launcher = self._read_launcher_latest()
        client_outdated = self._client_outdated(client)
        launcher_outdated = self._launcher_outdated(launcher)
        result.data["update_available"] = client_outdated or launcher_outdated
        # Name the version of what Update would replace, not always the client's.
        if client_outdated and client is not None and client.get("version"):
            result.data["latest_version"] = client["version"]
        elif launcher_outdated and launcher is not None and launcher.get("version"):
            result.data["latest_version"] = launcher["version"]

    def _mark_update_incomplete(self, marker: Path | None = None) -> Path:
        """Write the "update unfinished" marker; raises BackendError when it cannot."""
        marker = marker or self._update_marker()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            # The folder is the game's (writable from the Sandbox): never follow a link there.
            if marker.is_symlink():
                marker.unlink()
            # Nor block on a FIFO it left there: non-blocking, and only a regular file counts.
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(f"{marker.name} is not a regular file")
            finally:
                os.close(fd)
        except OSError as exc:
            raise BackendError(f"cannot write to the NIKKE folder: {exc}") from exc
        return marker

    def _update_client_direct(self, *, missing: bool = False) -> None:
        """Download what changed in the game client; nothing of the publisher's runs.

        ``missing``: there is no client yet (a launcher-only install), so all of it comes now.
        """
        what = (
            "downloading the game client (about 1.1 GB); the launcher is not needed"
            if missing
            else "updating the game client"
        )
        self.ctx.emit(Event("step", f"{self.display_name}: {what}"))
        marker = self._mark_update_incomplete()
        try:
            nikke_source.install_game(
                self._launcher_root(), emit=self.ctx.emit, cancel=self.ctx.cancel
            )
        except (railrepo.RailError, OSError) as exc:
            thing = "game client" if missing else "update"
            raise BackendError(
                f"the NIKKE {thing} could not be downloaded ({exc}). Try again later, or turn off "
                '"Start the game without the NIKKE launcher" and press Play to let the launcher '
                f"{'download' if missing else 'update'} the game."
            ) from exc
        marker.unlink(missing_ok=True)
        try:
            self._fetch_client_latest()  # fresh: Update just asked the publisher anyway
        except (railrepo.RailError, OSError, ValueError) as exc:
            log.debug("NIKKE: could not refresh the client version: %s", exc)

    def update(self) -> None:
        if not self._launcherless_wanted():
            super().update()  # the launcher updates the game
            return
        self.ctx.cancel.raise_if_cancelled()
        if not self._client_exe().is_file():
            raise BackendError("NIKKE is not installed yet")
        # Files are replaced one by one: never under the running game or launcher (#933).
        if (
            scan_pids(self._patterns())
            or scan_pids(ProcessQuery(prefix=str(self.prefix())))
            or self._running_elsewhere()  # another Flatpak instance's game (run lock)
        ):
            raise BackendError("Stop the game before updating NIKKE", 8)
        self._update_client_direct()
        self._update_launcher_if_needed(fetch=True)

    def _update_before_play(self) -> None:
        """A launcherless Play on an outdated client updates it first (a fresh check).

        The same rules as Update: never under a running game or launcher (here, in another
        Nadare window or from a desktop shortcut), and only while holding the profile's
        operation lock, so another window's Remove or Update cannot run at the same time.
        """
        if self._running_elsewhere():
            # Another instance runs this game (its launch then waits for the run lock): never
            # replace its files now. The update happens on a later Play.
            self.info("NIKKE is running in another Nadare window; not updating it now")
            return
        if scan_pids(self._patterns()) or scan_pids(ProcessQuery(prefix=str(self.prefix()))):
            self.info("NIKKE or its launcher is already running; not updating the game now")
            return
        lock = process.OperationLock(self.ctx.name)
        try:
            lock.acquire()
        except process.ProfileLockError:
            if not process.operation_in_progress(self.ctx.name):
                # flock does not work on this state folder (ENOLCK/ENOTSUP): nobody holds the
                # game, the lock just cannot be had. Play what is installed (round 6).
                self.info("cannot lock the NIKKE profile here; not updating the game now")
                return
            # Another window is installing, updating or removing these files: neither update
            # them nor start the game on them now (round 5).
            raise BackendError(
                process.OPERATION_IN_PROGRESS.format(name=self.display_name)
            ) from None
        try:
            self._update_before_play_locked()
        finally:
            lock.release()

    def _client_missing_but_signed_in(self) -> bool:
        """The launcher is installed and a sign-in is saved, but the game client is nowhere.

        Only then is a client download worth it: without a sign-in the launcher opens anyway
        (and downloads the game itself), and a client found on another drive is used as it is.
        """
        launcher = self.find_launcher()
        if launcher is None or self._client_exe().is_file():
            return False
        # The download goes to _launcher_root(); find_game looks next to the launcher. Only when
        # those agree does the downloaded client get found (an unusual layout keeps the launcher).
        if launcher.parent.parent.resolve() != self._launcher_root().resolve():
            return False
        if nikke_sail.find_game(launcher, self.prefix()) is not None:
            return False
        return self._launcherless_cache() is not None

    def _update_before_play_locked(self) -> None:
        self._update_client_before_play()
        checks = bool(self.ctx.config.data.get("check_updates", True))
        self._update_launcher_if_needed(fetch=checks)

    def _update_client_before_play(self) -> None:
        if not self._client_exe().is_file() and self._client_missing_but_signed_in():
            self._update_client_direct(missing=True)
            return
        if self._update_marker().exists():
            self.info("finishing the interrupted NIKKE update first")
            self._update_client_direct()
            return
        if not self.ctx.config.data.get("check_updates", True):
            return  # the user turned update checks off: play what is installed
        try:
            latest = self._fetch_client_latest()
        except (railrepo.RailError, OSError, ValueError) as exc:
            self.info(f"could not check for a NIKKE update ({exc}); starting the game as it is")
            return
        if self._client_outdated(latest):
            self._update_client_direct()

    # ------------------------------------------------------------- launcher (#1245)
    # Launcher-free play never runs the launcher, so its folder (with the intl_service sign-in
    # the launcher-free sign-in uses) would never update. Nadare keeps it current the same way
    # as the client: compare a few key files with the publisher's launcher manifest and, when
    # they differ, install_launcher over the folder (only changed files are fetched).

    def _launcher_dir(self) -> Path | None:
        """The launcher folder Nadare manages (``<root>/Launcher``); ``None`` if elsewhere."""
        launcher = self.find_launcher()
        if launcher is None:
            return None
        folder = launcher.parent
        try:
            if folder.resolve() != (self._launcher_root() / "Launcher").resolve():
                return None
        except OSError:
            return None
        return folder

    def _launcher_update_marker(self) -> Path:
        return self._launcher_root() / ".nadare-launcher-update-incomplete"

    def _launcher_latest_path(self) -> Path:
        return paths.cache_dir() / "nikke-latest" / f"{self.game_tag}-launcher.json"

    def _read_launcher_latest(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._launcher_latest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict) or not files:
            return None
        return payload

    def _fetch_launcher_latest(self) -> dict[str, Any]:
        """Ask the publisher for the current launcher's key files and cache them; raises."""
        info = nikke_source.launcher_info()
        entries = nikke_source.launcher_key_entries(info)
        latest = {
            "files": {
                rel: {"size": int(e.size), "md5": bytes(e.md5).hex()} for rel, e in entries.items()
            },
            "version": str(info.get("version_name") or ""),
            "checked_at": int(time.time()),
        }
        try:
            path = self._launcher_latest_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(latest), encoding="utf-8")
        except OSError as exc:
            log.debug("NIKKE: could not cache the launcher version: %s", exc)
        return latest

    def _launcher_outdated(self, latest: dict[str, Any] | None) -> bool:
        """Whether the launcher folder differs from the published launcher (or is unfinished)."""
        if not self._launcherless_wanted():
            return False
        if self._launcher_update_marker().exists():
            return True
        folder = self._launcher_dir()
        if folder is None or latest is None:
            return False
        for rel, expected in latest["files"].items():
            path = folder / rel
            try:
                if path.stat().st_size != int(expected["size"]):
                    return True
                digest = hashlib.md5()
                with open(path, "rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(block)
            except (OSError, KeyError, TypeError, ValueError):
                return True  # a key file is missing or unreadable: bring the folder current
            if digest.hexdigest() != str(expected["md5"]):
                return True
        return False

    def _update_launcher_direct(self, folder: Path) -> None:
        """Fetch what changed in the launcher folder; nothing of the publisher's runs."""
        self.ctx.emit(
            Event("step", f"{self.display_name}: updating the launcher's sign-in files "
                  "(the launcher itself is not started)")
        )
        marker = self._mark_update_incomplete(self._launcher_update_marker())
        try:
            nikke_source.install_launcher(folder, emit=self.ctx.emit, cancel=self.ctx.cancel)
        except (railrepo.RailError, OSError) as exc:
            raise BackendError(
                f"the NIKKE launcher could not be updated ({exc}). Try again later, or turn off "
                '"Start the game without the NIKKE launcher" and press Play to let the launcher '
                "update itself."
            ) from exc
        marker.unlink(missing_ok=True)
        try:
            self._fetch_launcher_latest()
        except (railrepo.RailError, OSError, ValueError) as exc:
            log.debug("NIKKE: could not refresh the launcher version: %s", exc)

    def _update_launcher_if_needed(self, *, fetch: bool) -> None:
        """Bring the launcher folder current (launcher-free only; the caller holds the guards)."""
        if not self._launcherless_wanted():
            return
        folder = self._launcher_dir()
        if folder is None:
            return
        if self._launcher_update_marker().exists():
            self.info("finishing the interrupted NIKKE launcher update first")
            self._update_launcher_direct(folder)
            return
        if not fetch:
            return
        try:
            latest = self._fetch_launcher_latest()
        except (railrepo.RailError, OSError, ValueError) as exc:
            self.info(f"could not check the NIKKE launcher ({exc}); using it as it is")
            return
        if self._launcher_outdated(latest):
            self._update_launcher_direct(folder)

    # ------------------------------------------------------------- launcherless

    def _wayland_default(self) -> str:
        """Auto: Wayland for the game started without the launcher, X11 for everything else.

        X11 is NIKKE's default because the launcher's sign-in browser goes blank on native Wayland
        (#1124). The launcherless sign-in helper stays on X11 too: NIKKE's ``intl_service`` never
        answered it under Wine's Wayland driver (rig, 2026-11-10) and finished in seconds on X11.
        Only the game itself (the Sail host and ``nikke.exe``) gets Wayland, so the fallback to
        the launcher stays on X11. A Display choice in Settings always wins.
        """
        choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        if choice in ("", "auto") and getattr(self, "_launcherless_game", False):
            return "1"
        return super()._wayland_default()

    def _launcherless_wanted(self) -> bool:
        return bool(as_bool(profile_setting(self.ctx.profile, "launcherless"), False))

    def _session_cache(self) -> Path:
        """Nadare's private copy of the INTL sign-in for this profile."""
        return self.ctx.data_dir() / "nikke-session"

    def login(self) -> None:
        """Let the INTL service run the Google sign-in in the user's browser."""
        launcher = self.find_launcher()
        service = launcher.parent / "intl_service" / "intl_service.exe" if launcher else None
        if launcher is None or service is None or not service.is_file():
            raise BackendError(
                "NIKKE is not installed yet; install it first, then press Log in"
            )
        prefix = self.prefix()
        storage = nikke_sail.cache_storage_dir(prefix)
        auth = storage / "intl_auth.txt"
        before = _file_key(auth)
        try:
            stage = nikke_sail.stage_hosts(prefix)
        except (OSError, nikke_sail.HostError) as exc:
            raise BackendError(f"cannot prepare the NIKKE sign-in: {exc}") from exc
        try:
            work = nikke_sail.private_workdir(stage)
        except OSError as exc:
            raise BackendError(f"cannot prepare the NIKKE sign-in folder: {exc}") from exc
        try:
            self.ctx.emit(
                Event(
                    "step",
                    f"{self.display_name}: the Google sign-in opens in your browser; "
                    "finish it there and come back",
                )
            )
            enc = work / "session.enc.json"
            cmd = nikke_sail.intl_command(
                str(stage / nikke_sail.INTL_HOST_NAME),
                nikke_sail.win_path(prefix, service),
                nikke_sail.win_path(prefix, enc),
                login_channel=6,
                auto_login=0,
                rsp_timeout_ms=600000,
            )
            argv, env = self._prefix_command(cmd, sandboxed=self.secure_mode_enabled())
            # The service opens Google itself: keep the link relay so the browser still opens.
            argv, env = self._secure_wrap(argv, env, subject="sign-in")
            self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
            code = stream(argv, env, on_line=self._on_line, cancel=self.ctx.cancel)
            if (
                code != 0
                or not enc.is_file()
                or enc.stat().st_size == 0
                or _file_key(auth) is None
                or _file_key(auth) == before
            ):
                raise BackendError(
                    "the Google sign-in did not finish "
                    f"(sign-in helper exit code {code}); please try again"
                )
            nikke_sail.save_cache(storage, self._session_cache())
        finally:
            self._stop_link_relay()
            shutil.rmtree(work, ignore_errors=True)
        self.ctx.emit(Event("done", "Logged in to NIKKE with Google"))

    @classmethod
    def forget_session_files(cls, name: str) -> bool:
        """Remove: drop Nadare's copy of the NIKKE sign-in (``<profile data>/nikke-session``).

        The dialog says the saved login goes; without this a profile added again under the same
        name would be signed in. Returns whether there was a copy. The prefix's own
        ``intl_auth.txt`` goes with the prefix when the user deletes it.
        """
        if not valid_profile_name(name):
            return False
        # The profile folder itself may be a link to another disk (BackendContext.data_dir
        # allows that); only the session folder below it must not be a link.
        cache = paths.profiles_dir() / name / "nikke-session"
        if cache.is_symlink():
            cache.unlink()
            return True
        if not cache.is_dir():
            return False
        shutil.rmtree(cache)
        return True

    def logout(self) -> None:
        cache = self._session_cache()
        if cache.is_symlink():
            # Never follow a link to delete what it points at; dropping the link is enough for
            # Nadare to forget the sign-in (#1207 b023: rmtree refused, and "Logged out" lied).
            with contextlib.suppress(OSError):
                cache.unlink()
        else:
            shutil.rmtree(cache, ignore_errors=True)
        try:
            auth = nikke_sail.cache_storage_dir(self.prefix()) / "intl_auth.txt"
            auth.unlink(missing_ok=True)
        except OSError:
            pass
        if cache.is_symlink() or nikke_sail.has_cache(cache) or _non_empty(cache / "intl_auth.txt"):
            raise BackendError(
                f"Could not remove the saved NIKKE sign-in at {cache}. Delete that folder "
                "yourself, then press Log out again."
            )
        self.ctx.emit(Event("done", "Logged out of NIKKE"))

    def status(self) -> Status:
        result = super().status()
        try:
            logged_in = any(
                nikke_sail.has_cache(cache) or _non_empty(cache / "intl_auth.txt")
                for cache in (
                    self._session_cache(),
                    nikke_sail.cache_storage_dir(self.prefix()),
                )
            )
        except Exception:  # noqa: BLE001 - status must never fail
            logged_in = False
        result.data["logged_in"] = logged_in
        # Log in runs the game's own INTL service: it needs the launcher installed.
        result.data["can_login"] = result.state != "not_installed"
        # Log in is the Google sign-in only (a Level Infinite account signs in once in the
        # launcher), so the button says which one it is (maintainer, 2026-11-11).
        result.data["login_label"] = "Log in with Google"
        self._client_update_status(result)
        self._tray_hint(result)
        return result

    def _launcher_age(self) -> float | None:
        """Seconds since the oldest running NIKKE launcher process started, or ``None``."""
        ages = [process_age_seconds(pid) for pid in scan_pids(self._patterns())]
        known = [age for age in ages if age is not None]
        return max(known) if known else None

    def _tray_hint(self, result: Status) -> None:
        """Say so when the launcher runs with no window (closed to the tray).

        On a desktop without a visible tray it seems to vanish while the page still shows it
        running. Telling is safe; stopping is not (the launcher downloads updates inside its
        own process), so auto-stop stays opt-in. X11 only, like the window check itself.
        """
        if not (result.running or result.state == "running"):
            return
        try:
            if not self._display_in_use().startswith("x11") or self._nikke_busy_now():
                return
            age = self._launcher_age()
            if age is None or age < TRAY_HINT_AFTER_SECONDS:
                return  # a launcher that just started has no window yet
            if x11windows.any_shown(("NIKKE",)) is not False:
                return  # shown, or the windows cannot be read
        except Exception:  # noqa: BLE001 - status must never fail
            return
        result.data["launcher_hidden"] = True
        result.message = (
            "The NIKKE launcher is still running in the background (its window was closed to "
            "the tray). Press Stop to end it, or use Exit in the launcher next time."
        )

    def _launcherless_cache(self) -> Path | None:
        """The saved sign-in, adopting the launcher's own cache on the first run.

        A sign-in done in the launcher lands in the prefix; copying it here means the
        launcherless path needs no second sign-in. ``None`` when neither has one.
        """
        cache = self._session_cache()
        stored = nikke_sail.cache_storage_dir(self.prefix())
        own_auth = cache / "intl_auth.txt"
        if nikke_sail.has_cache(cache) and (
            not nikke_sail.has_cache(stored) or not _newer(stored / "intl_auth.txt", own_auth)
        ):
            return cache
        if not nikke_sail.has_cache(stored):
            return cache if nikke_sail.has_cache(cache) else None
        # First run, or the user signed in again in the launcher (the advice when a saved
        # sign-in stops working): the newer sign-in in the prefix wins over Nadare's copy.
        try:
            nikke_sail.save_cache(stored, cache)
        except (OSError, nikke_sail.HostError) as exc:
            self.info(f"cannot keep the NIKKE sign-in from the launcher ({exc})")
            return None
        return cache

    def _no_launcherless(self, reason: str) -> None:
        """Say, where the user sees it, why "without the launcher" still opens the launcher."""
        self.info(reason)
        self.ctx.emit(Event("step", f"{self.display_name}: opening the launcher: {reason}"))

    def _launcherless_plan(self) -> nikke_sail.GameFiles | None:
        """The files a launcherless launch needs; ``None`` (with a reason) uses the launcher."""
        launcher = self.find_launcher()
        if launcher is None:
            self._no_launcherless("NIKKE is not installed yet; the launcher will download it")
            return None
        files = nikke_sail.find_game(launcher, self.prefix())
        if files is None:
            if self._launcherless_cache() is None:
                self._no_launcherless(
                    "the game is not downloaded and there is no sign-in yet; the launcher will do "
                    "both (or press Log in with Google first: then Play downloads the game itself)"
                )
            else:
                self._no_launcherless(
                    "the game is not installed yet; the launcher will download it"
                )
            return None
        if self._launcherless_cache() is None:
            self._no_launcherless("there is no NIKKE sign-in yet; the launcher will ask for one")
            return None
        try:
            nikke_sail.stage_hosts(self.prefix())
        except (OSError, nikke_sail.HostError) as exc:
            self._no_launcherless(f"cannot start NIKKE without its launcher ({exc})")
            return None
        return files

    def _launch_launcherless(self, files: nikke_sail.GameFiles) -> int:
        """Start the game through the two Sail hosts instead of ``nikke_launcher.exe``."""
        prefix = self.prefix()
        cache = self._launcherless_cache()
        if cache is None:  # the plan just checked it; a deleted cache is a real error
            raise BackendError("the saved NIKKE sign-in is gone; sign in in the launcher again")
        stage = prefix.joinpath(*nikke_sail.STAGE_PARTS)
        try:
            work = nikke_sail.private_workdir(stage)
        except OSError as exc:
            raise BackendError(f"cannot prepare the NIKKE session folder: {exc}") from exc
        self._launcherless_active = True
        try:
            try:
                nikke_sail.restore_cache(
                    cache, nikke_sail.cache_storage_dir(prefix), root=prefix
                )
            except (OSError, nikke_sail.HostError) as exc:
                raise BackendError(f"cannot restore the NIKKE sign-in: {exc}") from exc
            self._run_vcredist(created=False)
            self._watch = runtimewatch.RuntimeWatch()
            self._launch_started = time.monotonic()
            self.ctx.emit(
                Event("step", f"{self.display_name}: using the saved session, no launcher")
            )
            enc = work / "session.enc.json"
            cmd = nikke_sail.intl_command(
                str(stage / nikke_sail.INTL_HOST_NAME),
                nikke_sail.win_path(prefix, files.service),
                nikke_sail.win_path(prefix, enc),
            )
            argv, env = self._prefix_command(cmd, sandboxed=self.secure_mode_enabled())
            argv, env = self._secure_wrap(argv, env, subject="sign-in")
            self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
            code = stream(argv, env, on_line=self._on_line, cancel=self.ctx.cancel)
            if code != 0 or not enc.is_file() or enc.stat().st_size == 0:
                raise BackendError(
                    "NIKKE's saved sign-in could not be used. Turn off \"Start the game without "
                    "the NIKKE launcher\" and press Play to sign in again in the launcher. "
                    f"(sign-in helper exit code {code})"
                )

            resource = nikke_sail.win_path(prefix, files.resource_dir)
            session = nikke_sail.make_session_file(
                self._read_host_session(enc), work / "session.json", game_resource_path=resource
            )
            sail = nikke_sail.sail_command(
                str(stage / nikke_sail.SAIL_HOST_NAME),
                nikke_sail.win_path(prefix, session),
                nikke_sail.win_path(prefix, files.game_exe),
                nikke_sail.win_path(prefix, files.game_cwd),
                self._extra_args("launch_args"),
            )
            extra_env = nikke_sail.host_env(
                prefix,
                launcher_dir=nikke_sail.win_path(prefix, files.launcher_dir),
                log=nikke_sail.win_path(prefix, work / "sail_host.log"),
                base={},
            )
            self._launcherless_game = True  # only the game gets Wayland on Auto
            argv, env = self._prefix_command(
                sail, extra_env=extra_env, sandboxed=self.secure_mode_enabled()
            )
            argv, env = self._secure_wrap(argv, env)  # the game, like a launcher launch
            self.ctx.emit(Event("step", f"{self.display_name}: launching the game"))
            self._log_launch_details(env)
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = self._renderer_probe().start()
            try:
                with self._run_lock():
                    self._begin_game_output()
                    try:
                        code = stream(
                            argv, env, on_line=self._on_game_line, cancel=self.ctx.cancel
                        )
                    finally:
                        self._end_game_output()
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
            # The helpers are windowless, so their output reaches Nadare only through this file;
            # keep it after every run (which Sail commands the game sent), not only on failure.
            self._report_host_log(work / "sail_host.log", tail=None if code == 0 else 15)
            return self._checked_launch_exit(code)
        finally:
            self._launcherless_active = False
            self._launcherless_game = False
            self._stop_link_relay()
            shutil.rmtree(work, ignore_errors=True)

    def _read_host_session(self, path: Path) -> dict[str, Any]:
        try:
            auth = nikke_sail.read_auth(path)
            missing = [k for k in nikke_session.REQUIRED_AUTH_FIELDS if not auth.get(k)]
            if missing:
                raise nikke_sail.HostError(f"missing {', '.join(missing)}")
            return auth
        except (OSError, nikke_sail.HostError) as exc:
            raise BackendError(
                "the NIKKE sign-in returned no session. Turn off \"Start the game without the "
                "NIKKE launcher\" and press Play to sign in again in the launcher."
            ) from exc

    def _report_host_log(self, log: Path, *, tail: int | None = 15) -> None:
        """Show the Sail host's log (protocol only, never the session): the last ``tail`` lines,
        or all of it (capped at 500 lines) when ``tail`` is None."""
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines[-(tail or 500):]:
            self.info(f"sail: {line}")

    def _patterns(self, target: Path | None = None) -> ProcessQuery:
        """``super()``'s query, plus the game and the Sail host during a launcherless run."""
        query = super()._patterns() if target is None else super()._patterns(target)
        if not getattr(self, "_launcherless_active", False):
            return query
        names = tuple(dict.fromkeys((*query.names, "nikke.exe", nikke_sail.SAIL_HOST_NAME)))
        return dataclasses.replace(query, names=names)

    def _sandbox_game_dir(self) -> Path | None:
        """Nothing beyond the prefix: the launcher and the game both live inside it."""
        return None

    def _launcher_root(self) -> Path:
        """The folder whose ``Launcher`` subfolder holds the launcher."""
        target = self.install_dir()
        destination = endfield_shim.win_path(self.prefix(), target)
        if not destination.startswith("C:\\"):
            # Add Game gives every profile an install_dir outside its prefix, which the launcher
            # installers have always ignored: the launcher goes to the default C:\NIKKE inside the
            # prefix, and find_launcher() searches drive_c for it.
            target = self.prefix() / self.install_subdir
        return target

    def _install_launcher_directly(self) -> bool:
        """Download the launcher, then the game program; no installer runs.

        The launcher still downloads the ~23 GB of game resources itself (and does it
        again after a game patch); only the launcher and the game program come from here.
        """
        self._direct_done = False
        if not self._direct_wanted():
            return False
        self.ctx.emit(
            Event("step", "Downloading the NIKKE launcher (about 400 MB); no installer runs")
        )
        try:
            nikke_source.install_launcher(
                self._launcher_root() / "Launcher",
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
            )
        except (railrepo.RailError, OSError) as exc:
            self._direct_attempt_failed = True
            self.info(f"direct launcher download not available ({exc}); using the installer")
            return False
        if self.find_launcher() is None:
            self.info("the direct launcher download left no launcher; using the installer")
            return False
        self.ctx.emit(
            Event("step", "Downloading the NIKKE game client (about 1.1 GB); no installer runs")
        )
        # Like an update: until every file is in, nikke.exe alone cannot tell a complete client
        # (a cut download may already have written it). A launcherless Play finishes it first.
        self._mark_update_incomplete()
        try:
            nikke_source.install_game(
                self._launcher_root(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
            )
        except (railrepo.RailError, OSError) as exc:
            self._direct_attempt_failed = True
            self.info(f"direct game client download not available ({exc}); using the installer")
            return False
        self._update_marker().unlink(missing_ok=True)
        self._direct_done = True
        return True

    def _run_installer(self, installer: Path) -> int:
        """Install without a window when possible; otherwise the installer window as before."""
        self._silent_done = False
        # An explicit Sandbox on that cannot run must stop the install before anything runs;
        # the silent path would otherwise swallow the error and try the window instead.
        if self.secure_mode_enabled():
            self._sandbox_check("installer")
        if self._silent_wanted():
            try:
                code = self._run_silent(installer)
            except (SilentInstallUnavailable, OSError, BackendError) as exc:
                self.info(f"silent install not available ({exc}); using the installer window")
            else:
                # An exit code of 0 and a launcher on disk: a launcher left by an earlier install
                # must not make a failed run look like a success.
                if code == 0 and self.find_launcher() is not None:
                    self._silent_done = True
                    return code
                self.info(
                    f"the silent install did not finish (exit code {code}); "
                    "using the installer window"
                )
            # Say why a window appears after Nadare promised none (#1000).
            self.ctx.emit(
                Event(
                    "step",
                    f"Installing the {self.display_name} launcher without a window did not work "
                    "here, so its installer window opens next.",
                )
            )
        return super()._run_installer(installer)

    def install_done_message(self) -> str:
        if getattr(self, "_silent_done", False) or getattr(self, "_direct_done", False):
            return (
                f"{self.display_name} launcher installed. Press Play to open it and sign in; "
                "it downloads the rest of the game (about 23 GB)."
            )
        return super().install_done_message()

    def _run_silent(self, installer: Path) -> int:
        sevenz = sevenzip.find()
        if not sevenz:
            raise SilentInstallUnavailable("7-Zip not found")
        target = self._launcher_root()
        destination = endfield_shim.win_path(self.prefix(), target)
        folder = _wine_user_dir(self.prefix()) / "AppData" / "Local" / "nikkeminiloader"
        # Nadare's own unpack folder, started clean: a downloader left by an earlier version must
        # never be mistaken for the one in this installer.
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)
        elif folder.exists() or folder.is_symlink():
            folder.unlink(missing_ok=True)
        _extract_nsis(sevenz, installer, folder)
        loader = _find_inner_loader(folder)
        command = [self.SILENT_SWITCHES[0], f"/Dst={destination}", self.SILENT_SWITCHES[1]]
        helper = _stage_quiet_helper(folder)
        if helper is not None:
            # The helper runs inside Wine, so it is told the loader's Windows path.
            command = [str(helper), endfield_shim.win_path(self.prefix(), loader), *command]
        else:
            self.info("window-hiding helper unavailable; the downloader window will be visible")
            command = [str(loader), *command]
        argv, env = self._prefix_command(command, sandboxed=self.secure_mode_enabled())
        argv, env = self._secure_wrap(argv, env, subject="installer", links=False)
        self.ctx.emit(
            Event("step", f"Installing the {self.display_name} launcher (no window, about 700 MB)")
        )
        self.ctx.emit(
            Event(
                "progress",
                f"{self.display_name} launcher download",
                data={"indeterminate": True},
            )
        )
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        stop = threading.Event()
        # The size before the downloader starts: measured in the thread, a busy machine could
        # start the watcher after the first files were written and never show any growth.
        start = _folder_bytes(target)
        watcher = threading.Thread(
            target=self._watch_silent_install, args=(target, stop, start), daemon=True
        )
        watcher.start()
        try:
            return _arknights.stream(argv, env, on_line=self._on_line, cancel=self.ctx.cancel)
        finally:
            stop.set()
            watcher.join(timeout=5)

    def _watch_silent_install(self, target: Path, stop: threading.Event, start: int) -> None:
        """Turn the hidden downloader's growing launcher folder into a progress bar (#1000).

        Only ``target`` is measured (links are not followed). The bar stays a busy bar until the
        folder grows, and never reaches 100 % on its own: the step ends when the downloader does.
        """
        while not stop.wait(_SILENT_POLL_SECONDS):
            grown = _folder_bytes(target) - start
            if grown <= 0:
                continue
            fraction = min(0.99, grown / _SILENT_LAUNCHER_BYTES)
            if stop.is_set():
                break
            try:
                self.ctx.emit(
                    Event("progress", f"{self.display_name} launcher download", progress=fraction)
                )
            except ValueError:
                break

    def _prefix_command(
        self,
        args: list[str],
        extra_env: dict[str, str] | None = None,
        *,
        sandboxed: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        argv, env = super()._prefix_command(args, extra_env, sandboxed=sandboxed)
        env["GAMEID"] = self._explicit_env("GAMEID", extra_env) or self.game_id
        env["UMU_ID"] = self._explicit_env("UMU_ID", extra_env) or env["GAMEID"]
        # The NIKKE launcher locks its repair, download and update features when it thinks Steam
        # started it. DW-Proton strips these in its own fix; do it here too so any runner works.
        for name in self.STEAM_VARIABLES:
            if not self._explicit_env(name, extra_env):
                env.pop(name, None)
        return argv, env

    def _explicit_env(self, key: str, extra_env: dict[str, str] | None) -> str | None:
        """A value the profile or caller set on purpose (not Nadare's own default)."""
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        for source in (extra_env, profile_env if isinstance(profile_env, dict) else None):
            if source and source.get(key):
                return str(source[key])
        return None


__all__ = ["LauncherBackend"]
