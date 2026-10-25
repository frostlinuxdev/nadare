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

import json
import logging
import os
import shlex
import shutil
import tempfile
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core import (
    diskspace,
    download,
    graphics,
    hostlibs,
    paths,
    prefixmove,
    process,
    runners,
    runtimewatch,
    sandbox,
    umu,
    vcredist,
    yostar,
)
from ..core.backends import (
    BackendContext,
    BackendError,
    BackendUnsupported,
    GameBackend,
    Option,
    Status,
)
from ..core.events import Event, mask
from .arknights_news import IMAGE_HOSTS, fetch_arknights_news
from .wine import (
    ProcessQuery,
    first_wine,
    path_option,
    profile_setting,
    scan_pids,
    stop_pids,
    stop_runner_wineserver,
    stream,
)

KIND = "arknights-yostar"

log = logging.getLogger(__name__)

#: Official endpoint returning the current Windows client download link.
INSTALLER_API = "https://www.arknights.global/api/resource/game-download-link?index=1&size=999"

#: Pinned fallback used when the profile and the API both fail to yield a URL.
#: TODO: refresh this when Yostar ships a new launcher release.
DEFAULT_INSTALLER_URL = (
    "https://launcher-pkg-ark-en.yo-star.com/install_pkg/game_launcher/"
    "Arknights_EN/Arknights_EN_Gamelauncher-1.8.1-setup.exe"
)

#: Default launcher tree under the prefix (the launcher's own default sub-path
#: is ``YostarGames/Arknights_EN``).
DEFAULT_INSTALL_SUBDIR = Path("drive_c/YostarGames/Arknights_EN")

#: Candidate launcher executable names, preferred first.  The guide's client
#: used ``game launcher.exe``; newer builds ship ``Arknights_EN_Gamelauncher.exe``.
LAUNCHER_NAMES = (
    "game launcher.exe",
    "Arknights_EN_Gamelauncher.exe",
    "Arknights_EN.exe",
    "Arknights.exe",
    "launcher.exe",
)

#: Bounded discovery: how deep and how many directories to walk in the prefix.
_SEARCH_MAX_DEPTH = 4
_SEARCH_MAX_DIRS = 2000


#: Profile key recording the prefix an install actually used. Uninstall only
#: removes a prefix inside the profile data dir or one that matches this record,
#: so a crafted/imported ``prefix`` cannot delete an unrelated tree.
INSTALLED_PREFIX_KEY = "installed_prefix"

#: Present in ``install_dir`` while a direct sync runs and left behind when it is
#: interrupted. Status and verify treat it as "incomplete", so a cancelled or
#: failed install/update is not mistaken for a finished one just because the main
#: executable is already on disk. Installs from before this existed have no
#: marker and still count as installed.
INSTALL_MARKER = ".nadare-install-incomplete"

#: Written after a successful direct sync: the manifest version actually installed.
VERSION_FILE = ".nadare-version.json"

#: How long a fetched published version stays fresh (6 hours).
UPDATE_CHECK_INTERVAL = 6 * 60 * 60


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (temporary file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _dangerous_removal_reason(
    target: Path, data_dir: Path, recorded: str | None = None
) -> str | None:
    """Why ``target`` must never be removed, or ``None`` when it is allowed.

    ``prefix`` is user-controlled, so removal follows a coherent policy: a
    prefix is removable only when it is inside this profile's data dir, or it
    matches the prefix recorded at install time (``INSTALLED_PREFIX_KEY``).
    Everything else -- the filesystem root, the home directory, this profile's
    data dir or one of its ancestors, another Nadare profile's tree -- is
    refused so a crafted/imported ``prefix`` cannot delete unrelated data.
    """
    resolved = target.resolve()
    if resolved.parent == resolved:
        return "the filesystem root"
    home = Path.home().resolve()
    if resolved == home:
        return "the home directory"
    data_resolved = data_dir.resolve()
    if resolved == data_resolved or resolved in data_resolved.parents:
        return f"the profile data directory {data_dir} (or one of its parents)"
    if data_resolved in resolved.parents:
        # Owned by this profile.
        return None
    profiles_resolved = paths.profiles_dir().resolve()
    if resolved == profiles_resolved or profiles_resolved in resolved.parents:
        return "another Nadare profile directory"
    if recorded:
        try:
            if resolved == Path(recorded).expanduser().resolve():
                return None
        except OSError:
            pass
    return "not a prefix this profile installed"


def _purge_target(data_dir: Path, recorded_prefix: str | None = None) -> Path | None:
    """The resolved directory a ``purge`` may remove, or raise ``BackendError``.

    ``None`` means the profile dir is a symlink to a directory that merely
    *contains* the recorded prefix: that directory may hold unrelated data, so
    the caller drops the link and leaves the target alone.

    ``data_dir`` may be a symlink (a profile relocated to another disk), which
    ``shutil.rmtree`` refuses; the resolved target is returned instead. The same
    ownership policy as prefix removal applies: only the profile's own directory
    is removed, or a relocation the profile can prove it installed into (its
    recorded ``installed_prefix`` resolves inside it). Anything else -- the
    filesystem root, the home directory, another profile, or an arbitrary
    symlink target such as ``/opt`` -- is refused instead of being ``rmtree``'d.
    """
    resolved = data_dir.resolve()
    if resolved.parent == resolved:
        raise BackendError(f"refusing to purge {data_dir}: it resolves to the filesystem root")
    if resolved == Path.home().resolve():
        raise BackendError(f"refusing to purge {data_dir}: it resolves to the home directory")
    expected = data_dir.parent.resolve() / data_dir.name
    if resolved == expected:
        return resolved
    profiles_resolved = paths.profiles_dir().resolve()
    if resolved == profiles_resolved or profiles_resolved in resolved.parents:
        raise BackendError(
            f"refusing to purge {data_dir}: it resolves to another profile directory"
        )
    if recorded_prefix:
        try:
            prefix_resolved = Path(recorded_prefix).expanduser().resolve()
        except OSError:
            prefix_resolved = None
        if prefix_resolved is not None:
            if prefix_resolved == resolved:
                return resolved
            if resolved in prefix_resolved.parents:
                return None
    raise BackendError(f"refusing to purge {data_dir}: it resolves outside the profile directory")


class ArknightsBackend(GameBackend):
    unofficial_notice = True
    install_size_hint = int(3.5 * 1024**3)

    #: Display driver when the profile says ``auto``: ``wayland`` or ``x11``.
    default_display = "wayland"
    #: Default ``DXVK_CONFIG``. DXVK 3.1.1's VK_EXT_present_timing path makes
    #: the Arknights client hang on its first present (issue #136); older DXVK
    #: ignores the unknown key. A profile ``extra_env`` DXVK_CONFIG overrides it.
    dxvk_config = "dxvk.enablePresentTiming = False"
    kind = KIND

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

    def __init__(self, ctx: BackendContext) -> None:
        super().__init__(ctx)
        self._watch = runtimewatch.RuntimeWatch()

    def _on_line(self, line: str) -> None:
        self._watch.feed(line)
        self.info(mask(line))

    # ---- paths / profile resolution ---------------------------------------
    def _data_dir(self) -> Path:
        return self.ctx.data_dir()

    def prefix(self) -> Path:
        configured = path_option(
            profile_setting(self.ctx.profile, "prefix")
            or profile_setting(self.ctx.profile, "wineprefix")
        )
        return configured if configured is not None else self._data_dir() / "prefix"

    def install_dir(self) -> Path:
        configured = path_option(profile_setting(self.ctx.profile, "install_dir"))
        if configured is not None:
            return configured
        return self.prefix() / self.install_subdir

    def cache_dir(self) -> Path:
        return paths.cache_dir() / KIND

    def runner_id(self) -> str:
        raw = profile_setting(self.ctx.profile, "proton_runner") or profile_setting(
            self.ctx.profile, "runner"
        )
        if not raw:
            return ""
        rid = str(raw).strip()
        return "" if rid.lower() in ("wine", "system", "none") else rid

    def _default_runner_applies(self) -> bool:
        """True when this profile should use the pinned DW-Proton by default.

        Whenever it names no runner (``wine``/``system``/``none`` count as unset)
        and no ``wine_binary``. An explicit ``wine_binary`` is still honoured.
        """
        if self.runner_id() or profile_setting(self.ctx.profile, "wine_binary"):
            return False
        return True

    def _wanted_family(self) -> str | None:
        """The runner family this profile gets from Nadare: a named family id
        (``dwproton``/``ge``/``cachyos``), or the default one; ``None`` otherwise."""
        rid = self.runner_id()
        if rid in runners.RUNNERS:
            return rid
        if not rid and self._default_runner_applies():
            return runners.DEFAULT_FAMILY
        return None

    def _ensure_default_runner(self, *, strict: bool) -> None:
        """Reuse the pinned build of the wanted family, or download it once.

        ``strict`` raises when it cannot be set up (launch needs it); otherwise a
        status notice is emitted and the caller carries on (a direct install only
        syncs files and needs no runtime until launch). A cancel passes through.
        """
        family = self._wanted_family()
        if family is None or runners.installed_pinned(family) is not None:
            self._runner_setup_error = None
            return
        self.ctx.emit(Event("step", "Setting up the Proton runner (one-time download)"))
        try:
            runners.ensure_pinned(family, emit=self.ctx.emit, cancel=self.ctx.cancel)
        except (runners.RunnerError, download.DownloadError, BackendError, OSError) as exc:
            self._runner_setup_error = exc
            if strict:
                if isinstance(exc, BackendError):
                    raise
                raise BackendError(f"could not set up the Proton runner: {exc}") from exc
            self.ctx.emit(
                Event(
                    "status",
                    f"Proton runner not set up yet ({exc}); it will be tried again at launch",
                )
            )
        else:
            self._runner_setup_error = None

    def resolve_runner(self) -> tuple[str | None, str]:
        """Return ``(path, kind)`` with kind ``proton`` or ``wine``.

        Never downloads (status polls call this): an unset runner resolves to the
        pinned DW-Proton only when it is already installed.
        """
        rid = self.runner_id()
        family = self._wanted_family()
        if family is not None:
            pinned = runners.installed_pinned(family)
            return (str(pinned), "proton") if pinned is not None else (None, "wine")
        if not rid:
            return None, "wine"
        try:
            found = runners.resolve_runner(rid)
        except runners.RunnerError as exc:
            raise BackendError(str(exc)) from exc
        if found is None:
            return None, "wine"
        return str(found), "proton"

    # ---- installer URL -----------------------------------------------------
    def _profile_installer_url(self) -> str | None:
        raw = profile_setting(self.ctx.profile, "installer_url") or profile_setting(
            self.ctx.profile, "installer"
        )
        if raw in (None, ""):
            return None
        return str(raw).strip() or None

    @staticmethod
    def _is_direct_https_exe(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(str(url))
        except ValueError:
            return False
        if parsed.scheme != "https" or not parsed.netloc:
            return False
        return Path(urllib.parse.unquote(parsed.path)).name.lower().endswith(".exe")

    def installer_url(self, *, resolve: bool = False) -> str:
        """Return the installer URL (profile override, resolved API, default)."""
        configured = self._profile_installer_url()
        if configured:
            return configured
        if resolve:
            fetched = self._fetch_installer_url()
            if fetched:
                return fetched
        return self.default_installer_url

    def _installer_sha256(self) -> str | None:
        """The pinned installer digest, validated, or ``None`` when unset."""
        raw = profile_setting(self.ctx.profile, "installer_sha256")
        if raw in (None, ""):
            return None
        text = str(raw).strip().lower()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise BackendError(
                f"installer_sha256 must be a 64-character hex SHA-256, got {raw!r}"
            )
        return text

    def _installer_is_usable(self, url: str) -> bool:
        """Whether ``url`` may be executed: an https .exe that is pinned or on
        Yostar's own hosts. Without a pinned digest an arbitrary host is refused.
        """
        if not self._is_direct_https_exe(url):
            return False
        if self._installer_sha256() is not None:
            return True
        return self._installer_host_trusted(url)

    def _installer_host_trusted(self, url: str) -> bool:
        """Whether an unpinned installer may come from ``url``'s host (Yostar's)."""
        return yostar.base_url_trusted(url)

    def require_installer_url(self, *, resolve: bool = False) -> str:
        url = self.installer_url(resolve=resolve)
        if not self._installer_is_usable(url):
            raise BackendError(
                f"no usable {self.display_name} installer URL: set profile 'installer_url' to "
                "the official Windows client .exe link on the publisher's host (or pin its "
                "'installer_sha256'); the pinned default may be stale (see TODO)"
            )
        return url

    def _fetch_installer_url(self, *, timeout: int = 30) -> str | None:
        if not self.installer_api:
            return None
        try:
            payload = download.fetch_json(self.installer_api, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - fall back to the pinned URL
            self.info(f"could not resolve the installer link ({exc}); using the pinned URL")
            return None
        try:
            url = str(payload["data"]["rows"][0]["client"]).strip()
        except (KeyError, IndexError, TypeError):
            return None
        return url if self._installer_is_usable(url) else None

    @staticmethod
    def _installer_filename(url: str) -> str:
        name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        return name or "Arknights_EN_Gamelauncher-setup.exe"

    def _download_installer(self, url: str) -> Path:
        dest = self.cache_dir() / self._installer_filename(url)
        pinned = self._installer_sha256()
        # An unpinned installer is only trusted by its host, so a redirect must
        # stay on Yostar's hosts; a pinned one is verified by its digest.
        redirect_allowed = None if pinned else self._installer_host_trusted
        size = download.remote_size(url, redirect_allowed=redirect_allowed)
        if size:
            diskspace.check_space(dest.parent, size)
        self.ctx.emit(
            Event("step", f"Downloading the {self.display_name} PC client installer ({dest.name})")
        )
        result = download.download(
            url,
            dest,
            expected_sha256=pinned,
            redirect_allowed=redirect_allowed,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )
        return result.path

    # ---- direct mode (CI-signed manifest) ---------------------------------
    def manifest_source(self) -> str:
        """The configured manifest URL/file, or ``""`` for the official path."""
        return yostar.configured_manifest(self.ctx.profile)

    def direct_mode(self) -> bool:
        return bool(self.manifest_source())

    def direct_target(self) -> Path | None:
        """The game exe a direct install/launch uses (profile override wins)."""
        raw = profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        )
        if raw:
            return path_option(raw, base=self.install_dir())
        return self.install_dir() / self.direct_exe

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

    def _install_marker_path(self) -> Path:
        return self.install_dir() / INSTALL_MARKER

    def _install_incomplete(self) -> bool:
        return self._install_marker_path().is_file()

    # ---- recorded / published versions ------------------------------------
    def _version_path(self) -> Path:
        return self.install_dir() / VERSION_FILE

    def _read_version_file(self) -> dict[str, Any] | None:
        """The version record from the last successful sync, or ``None``.

        Missing, unreadable or malformed files are treated as "not recorded"
        (installs from before this existed have no file).
        """
        try:
            payload = json.loads(self._version_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            return None
        return payload

    def _write_version_file(self, version: str, game_tag: str) -> None:
        _atomic_write_json(
            self._version_path(),
            {
                "version": str(version),
                "game_tag": str(game_tag),
                "synced_at": datetime.now(UTC).isoformat(),
            },
        )

    def _latest_path(self) -> Path:
        return paths.cache_dir() / "yostar-latest" / f"{self.game_tag}.json"

    def _read_latest(self) -> dict[str, Any] | None:
        """The cached published version, or ``None`` when absent/corrupt."""
        try:
            payload = json.loads(self._latest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            return None
        return payload

    def _write_latest(self, version: str) -> None:
        _atomic_write_json(
            self._latest_path(),
            {"version": str(version), "checked_at": int(time.time())},
        )

    def _record_synced_version(self, manifest: yostar.Manifest) -> None:
        """Remember what a finished sync installed and publish it as latest."""
        try:
            self._write_version_file(manifest.version, manifest.game_tag)
            self._write_latest(manifest.version)
        except OSError as exc:
            # A read-only install tree must not turn a finished update into a failure.
            log.debug("%s: could not record the synced version: %s", self.display_name, exc)

    def check_update(self) -> None:
        """Refresh the cached published version (direct mode only, offline-safe).

        Never raises: a manifest/network failure only leaves the cache untouched.
        """
        if not self.direct_mode():
            return
        try:
            if not self.ctx.config.data.get("check_updates", True):
                return
        except AttributeError:
            pass
        cached = self._read_latest()
        if cached is not None:
            try:
                age = time.time() - float(cached.get("checked_at", 0))
            except (TypeError, ValueError):
                age = UPDATE_CHECK_INTERVAL
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
            manifest = self._load_manifest()
        except (BackendError, RuntimeError, OSError) as exc:
            log.debug("%s: update check failed: %s", self.display_name, exc)
            return
        try:
            self._write_latest(manifest.version)
        except OSError as exc:
            log.debug("%s: could not cache the published version: %s", self.display_name, exc)

    def _missing_entries(self) -> list[Any]:
        """Files a direct install still lacks or has damaged (each has ``.path``)."""
        return list(yostar.plan(self._load_manifest(), self.install_dir()))

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

    def _renderer_probe(self) -> graphics.RendererProbe:
        return graphics.RendererProbe(
            lambda: scan_pids(self._patterns()),
            on_result=lambda label: self.info(f"renderer: {label}"),
        )

    # ---- launch-time additions (login tokens and the like) -----------------
    def launch_extra_args(self) -> list[str]:
        """Arguments appended after the profile's ``launch_args`` (default: none)."""
        return []

    def launch_extra_env(self) -> dict[str, str]:
        """Environment added for the game process (default: none)."""
        return {}

    def launch_secrets(self) -> tuple[str, ...]:
        """Values that must never appear in a log line (default: none)."""
        return ()

    def _redacted(self, text: str) -> str:
        for secret in self.launch_secrets():
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def _direct_argv(self, target: Path) -> list[str]:
        """The program and arguments handed to umu-run for a direct launch."""
        return [str(target), *self._extra_args("launch_args"), *self.launch_extra_args()]

    def _launch_direct(self) -> int:
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._run_vcredist(created=False)
        argv, env = self._prefix_command(
            self._direct_argv(target),
            self.launch_extra_env() or None,
        )
        self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
        self.info(self._runtime_note())
        self.info(self._redacted("exec: " + " ".join(shlex.quote(part) for part in argv)))
        _renderer, note = graphics.effective_renderer(self.ctx.profile)
        if note:
            self.info(note)
        probe = self._renderer_probe().start()
        try:
            return stream(
                argv, env, on_line=self._on_line, cancel=self.ctx.cancel
            )
        finally:
            probe.stop()
            probe.join(timeout=1.0)
            self._renderer = probe.result

    # ---- launcher discovery ------------------------------------------------
    def _explicit_target(self) -> Path | None:
        raw = profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        )
        if not raw:
            return None
        base = self.install_dir() if self.install_dir().is_dir() else self.prefix()
        return path_option(raw, base=base)

    def _search_launcher(self, root: Path) -> Path | None:
        if not root.is_dir():
            return None
        wanted = {name.lower() for name in self.launcher_names}
        visited = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and visited < _SEARCH_MAX_DIRS:
            directory, depth = stack.pop()
            visited += 1
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and entry.name.lower() in wanted:
                    return entry
            if depth < _SEARCH_MAX_DEPTH:
                stack.extend((entry, depth + 1) for entry in entries if entry.is_dir())
        return None

    def find_launcher(self) -> Path | None:
        """Return the launcher exe, or ``None`` when it cannot be found."""
        explicit = self._explicit_target()
        if explicit is not None:
            return explicit if explicit.is_file() else None
        for name in self.launcher_names:
            candidate = self.install_dir() / name
            if candidate.is_file():
                return candidate
        return self._search_launcher(self.prefix() / "drive_c")

    def _target(self) -> Path:
        target = self.find_launcher()
        if target is None:
            raise BackendError(f"{self.display_name} launcher not found under {self.install_dir()}")
        return target

    # ---- command construction ---------------------------------------------
    def _extra_args(self, key: str) -> list[str]:
        raw = profile_setting(self.ctx.profile, key)
        if isinstance(raw, str) and raw.strip():
            return shlex.split(raw)
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw]
        return []

    def _wayland_default(self) -> str:
        """``1`` for native Wayland, ``0`` for XWayland.

        ``display: wayland`` / ``x11`` in the profile wins; ``auto`` (the
        default) follows the backend's ``default_display``. Some newer Proton
        builds hang the Unity client on its first frame under native Wayland
        (NVIDIA), so ``display: x11`` is the escape hatch.
        """
        choice = str(profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        if choice in ("x11", "xwayland"):
            return "0"
        if choice == "wayland":
            return "1"
        return "1" if self.default_display == "wayland" else "0"

    def _runtime_note(self) -> str:
        """One log line naming the runner build and display driver in use."""
        proton, kind = self.resolve_runner()
        runner = runners.runner_version(Path(proton)).split(" ", 1)[-1] if proton else kind
        display = "wayland" if self._wayland_default() == "1" else "x11"
        return f"runner: {runner or kind}, display: {display}"

    def _prefix_command(
        self, args: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Build the Wine/``umu-run`` command and environment for ``args``."""
        prefix = self.prefix()
        env = process.build_env(None, extra={"WINEPREFIX": str(prefix)})
        proton, kind = self.resolve_runner()
        if kind == "proton":
            configured = str(profile_setting(self.ctx.profile, "umu_binary", "umu-run"))
            umu_run = umu.system(configured)
            missing = None
            if umu_run is None and not sandbox.in_flatpak():
                missing = hostlibs.missing_32bit()
                if missing:
                    umu_run = umu.resolve(
                        configured,
                        allow_download=True,
                        emit=self.ctx.emit,
                        cancel=self.ctx.cancel,
                    )
            env["PROTONPATH"] = str(proton)
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            env.setdefault("PROTON_ENABLE_WAYLAND", self._wayland_default())
            if umu_run:
                argv = [umu_run, *args]
            else:
                # No umu-run: run the Proton tree directly (like CZN), which needs the
                # host's 32-bit libraries unless we are inside the Flatpak runtime.
                if not sandbox.in_flatpak():
                    if missing is None:
                        missing = hostlibs.missing_32bit()
                    if missing:
                        raise BackendError(hostlibs.hint(missing))
                argv, env_updates = sandbox.proton_direct(proton, prefix, args)
                env.update(env_updates)
        else:
            # Plain Wine would otherwise ask to download wine-mono / wine-gecko.
            overrides = [p for p in str(env.get("WINEDLLOVERRIDES") or "").split(";") if p.strip()]
            if "mscoree,mshtml=" not in overrides:
                overrides.append("mscoree,mshtml=")
            env["WINEDLLOVERRIDES"] = ";".join(overrides)
            wine = profile_setting(self.ctx.profile, "wine_binary")
            resolved_wine = path_option(wine)
            if (
                resolved_wine is None
                and self._runner_setup_error is not None
                and not (prefix / "drive_c").is_dir()
            ):
                self.info(f"Proton runner setup failed: {self._runner_setup_error}")
                raise BackendError(
                    "The Proton runner could not be downloaded. Check your internet "
                    "connection and press Install again."
                ) from self._runner_setup_error
            argv = [
                str(resolved_wine) if resolved_wine is not None else str(first_wine()),
                *args,
            ]

        env.update(graphics.renderer_env(self.ctx.profile, kind=kind, environ=env))
        if self.dxvk_config and "DXVK_CONFIG" not in env:
            env["DXVK_CONFIG"] = self.dxvk_config  # profile extra_env still wins
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        if isinstance(profile_env, dict):
            env.update({str(k): str(v) for k, v in profile_env.items()})
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        return argv, process.sanitize_game_env(env)

    # ---- Visual C++ runtime (shared in nadare.core.vcredist) ----------------
    def _run_in_prefix(self, argv: list[str]) -> int:
        """Run ``argv`` inside this profile's Wine/Proton prefix."""
        cmd, env = self._prefix_command(list(argv))
        return stream(cmd, env, on_line=self._on_line, cancel=self.ctx.cancel)

    def _runtime_available(self) -> bool:
        """Whether a Wine/Proton runtime is explicitly configured for direct mode.

        Only an explicitly configured runner or Wine binary counts: a backend
        test that sets neither must never fall back to a real ``wine`` on PATH.
        """
        if self.runner_id():
            return True
        if profile_setting(self.ctx.profile, "wine_binary"):
            return True
        family = self._wanted_family()
        return family is not None and runners.installed_pinned(family) is not None

    def _run_vcredist(self, *, created: bool = False) -> None:
        """Best-effort VC++ runtime step (see :func:`nadare.core.vcredist.ensure`).

        Always attempted, on new and existing prefixes alike (it is not a user
        setting; a leftover ``"vcredist": false`` in a profile is ignored). It is
        a no-op once the real runtime is present and never aborts the caller.
        """
        vcredist.ensure(
            self.prefix(),
            profile=self.ctx.profile,
            cache_dir=self.cache_dir(),
            run_in_prefix=self._run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    def _ensure_direct_prefix(self) -> None:
        """Create the prefix for a direct install when a runtime is configured."""
        if (self.prefix() / "drive_c").is_dir():
            self._run_vcredist(created=False)
            return
        if self._runtime_available():
            self._ensure_prefix()

    def build_launch(self) -> tuple[list[str], dict[str, str], Path]:
        """Return ``(argv, env, target)`` for launching the client."""
        target = self._target()
        argv, env = self._prefix_command([str(target), *self._extra_args("launch_args")])
        return argv, env, target

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        prefix = self.prefix()
        direct = self.direct_mode()
        target = self.direct_target() if direct else self.find_launcher()
        incomplete = direct and self._install_incomplete()
        pids = scan_pids(self._patterns(target))
        data = {
            "prefix": str(prefix),
            "exe": str(target) if target else "",
            "runner": self.runner_id(),
            "pids": pids,
            "mode": "direct" if direct else "official",
            "can_repair": direct and self.install_dir().is_dir(),
            "renderer": getattr(self, "_renderer", None) or "",
        }
        version: str | None = None
        if direct:
            record = self._read_version_file()
            version = str(record["version"]) if record else None
            if version:
                data["version"] = version
            latest = self._read_latest()
            published = str(latest["version"]) if latest else None
            # Purely local: the published version is refreshed by check_update().
            data["update_available"] = bool(version and published and published != version)
        if direct and (target is None or not target.is_file() or incomplete):
            # Files may already be on disk from a cancelled/failed install, but
            # only a finished sync counts as installed.
            message = (
                "installation incomplete — click INSTALL to finish"
                if incomplete
                else f"{self.display_name} is not installed yet"
            )
            return Status(state="not_installed", version=version, message=message, data=data)
        if not direct and target is None:
            return Status(
                state="not_installed",
                version=version,
                message=f"{self.display_name} is not installed yet",
                data=data,
            )
        rid = self.runner_id()
        if rid and rid not in runners.RUNNERS:
            # An explicit runner that no longer resolves (e.g. its folder was
            # deleted) means the game cannot launch; the default-family runner
            # is downloaded on first launch, so it is never an error here.
            try:
                self.resolve_runner()
            except (BackendError, runners.RunnerError):
                return Status(
                    state="error",
                    version=version,
                    running=False,
                    message=(
                        "Game files are present but the Proton runner is missing: "
                        "choose or install a runner in Settings."
                    ),
                    data=data,
                )
        message = ""
        note = graphics.vulkan_note()
        if note:
            message = note
        return Status(
            state="running" if pids else "installed",
            version=version,
            running=bool(pids),
            message=message,
            data=data,
        )

    def _patterns(self, target: Path | None = None) -> ProcessQuery:
        """Build the structured process query for this profile's launcher."""
        extra = profile_setting(self.ctx.profile, "process_match")
        if target is None:
            target = self.find_launcher()
        names = (target.name,) if target is not None else ()
        return ProcessQuery(
            prefix=str(self.prefix()),
            names=names,
            match=str(extra) if extra else "",
        )

    def _ensure_prefix(self) -> bool:
        """Create the prefix; return True when the VC++ step already ran."""
        prefix = self.prefix()
        if (prefix / "drive_c").is_dir():
            return False
        # Only clean up a tree this call creates: an existing prefix (even a
        # half-built one the user pointed us at) is never deleted on failure.
        created = not prefix.exists()
        prefix.mkdir(parents=True, exist_ok=True)
        self.ctx.emit(Event("step", f"Creating the Wine prefix at {prefix}"))
        argv, env = self._prefix_command(["wineboot", "-u"])
        try:
            code = stream(
                argv, env, on_line=self._on_line, cancel=self.ctx.cancel
            )
            if code != 0 or not (prefix / "drive_c").is_dir():
                raise BackendError(
                    self._watch.explain(f"prefix creation failed (exit {code})")
                )
        except BaseException:
            # A cancelled/failed wineboot can leave drive_c behind, which would
            # make the next install treat the broken prefix as complete.
            if created:
                shutil.rmtree(prefix, ignore_errors=True)
            raise
        self._run_vcredist(created=True)
        return True

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self._ensure_default_runner(strict=False)
        # Record the prefix the install uses so uninstall may later remove an
        # explicitly configured external prefix (and only that one).
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        if self.direct_mode():
            self._ensure_direct_prefix()
            self._sync_direct()
            return
        url = self.require_installer_url(resolve=True)
        self.ctx.set("prefix", str(self.prefix()))
        if not self._ensure_prefix():
            self._run_vcredist(created=False)
        installer = self._download_installer(url)
        argv, env = self._prefix_command([str(installer), *self._extra_args("installer_args")])
        self.ctx.emit(Event("step", f"Running the {self.display_name} installer in {self.prefix()}"))
        self.ctx.emit(
            Event(
                "step",
                f"Finish the installer window; the {self.display_name} launcher will then download the game",
            )
        )
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        code = stream(
            argv, env, on_line=self._on_line, cancel=self.ctx.cancel
        )
        if self.find_launcher() is None:
            self.info(f"{self.display_name} installer exited with code {code} and left no launcher")
            raise BackendError(
                self._watch.explain(
                    f"The {self.display_name} installer did not finish. Try Install again. If it still "
                    "fails, open Settings → Runner and choose Proton-CachyOS."
                )
            )
        self.ctx.emit(Event("done", f"{self.display_name} installer finished"))

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        if self.direct_mode():
            target = self.direct_target()
            if target is None or not target.is_file():
                raise BackendError(f"{self.display_name} is not installed")
            self._sync_direct()
            return
        if self.find_launcher() is None:
            raise BackendError("the game is not installed")
        self.ctx.emit(Event("step", f"Starting the {self.display_name} launcher (it checks for updates)"))
        code = self.launch()
        if code != 0:
            raise BackendError(
                self._watch.explain(f"the {self.display_name} launcher exited with code {code}")
            )

    def repair(self) -> None:
        if not self.direct_mode():
            raise BackendUnsupported(
                "Repair is only available when Nadare manages the game files"
            )
        if not self.install_dir().is_dir():
            raise BackendError(f"{self.display_name} is not installed")
        self._sync_direct()
        try:
            missing = self._missing_entries()
        except (BackendError, yostar.ManifestError) as exc:
            if isinstance(exc, BackendError):
                raise
            raise self._manifest_error(exc) from exc
        if missing:
            names = ", ".join(entry.path for entry in missing[:5])
            if len(missing) > 5:
                names += f" (+{len(missing) - 5} more)"
            raise BackendError(
                f"{len(missing)} game file(s) are still damaged: {names}. "
                "Try again, or reinstall the game."
            )
        self.ctx.emit(Event("done", "All game files check out"))

    def remake_prefix(self) -> None:
        """Move the corrupted prefix aside and rebuild it, keeping the game files."""
        if scan_pids(self._patterns()):
            raise BackendError("Stop the game before rebuilding its Wine prefix", 8)
        # Make sure a runner exists BEFORE anything is moved: wineboot needs it.
        self._ensure_default_runner(strict=True)
        self.ctx.emit(Event("step", "Moving the old Wine prefix aside"))
        backup = prefixmove.move_aside(self.prefix())
        moved: list[Path] = []
        try:
            # _ensure_prefix creates the new prefix and runs wineboot; when it
            # returns False the VC++ step still has to run.
            if not self._ensure_prefix():
                self._run_vcredist(created=False)
            # An install_dir outside the prefix is left alone: nothing matches
            # ``relative_to(prefix)`` and restore_inside simply moves nothing.
            moved = prefixmove.restore_inside(backup, self.prefix(), [self.install_dir()])
        except BaseException:
            prefixmove.roll_back(backup, self.prefix(), moved)
            raise
        self.ctx.emit(
            Event(
                "done",
                f"The Wine prefix was rebuilt. The old one was kept at {backup}; "
                "delete it when you are sure everything works.",
            )
        )

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        # Fail "not installed" before spending a 320 MB download on a runner.
        if self.direct_mode():
            target = self.direct_target()
            if target is None or not target.is_file():
                raise BackendError(f"game executable not found: {target}")
        elif self.find_launcher() is None:
            raise BackendError("the game is not installed")
        self._ensure_default_runner(strict=True)
        if self.direct_mode():
            return self._launch_direct()
        self._run_vcredist(created=False)
        argv, env, target = self.build_launch()
        self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
        self.info(self._runtime_note())
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        _renderer, note = graphics.effective_renderer(self.ctx.profile)
        if note:
            self.info(note)
        probe = self._renderer_probe().start()
        try:
            return stream(
                argv, env, on_line=self._on_line, cancel=self.ctx.cancel
            )
        finally:
            probe.stop()
            probe.join(timeout=1.0)
            self._renderer = probe.result

    def stop(self) -> None:
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            stop_pids(pids)
        proton: str | None = None
        try:
            resolved, kind = self.resolve_runner()
        except BackendError:
            resolved, kind = None, "wine"
        if kind == "proton" and resolved:
            proton = resolved
        wine = path_option(profile_setting(self.ctx.profile, "wine_binary"))
        stop_runner_wineserver(prefix=self.prefix(), proton=proton, wine_binary=wine)

    def login(self) -> None:
        raise BackendUnsupported(
            "Arknights logs in inside the game; there is no separate login step"
        )

    def verify(self) -> dict[str, Any]:
        direct = self.direct_mode()
        target = self.direct_target() if direct else self.find_launcher()
        checks = {
            "prefix": (self.prefix() / "drive_c").is_dir(),
            "launcher": target is not None and target.is_file()
            if direct
            else target is not None,
        }
        missing: list[Any] = []
        manifest_note = ""
        if direct:
            checks["installation"] = not self._install_incomplete()
            if checks["installation"] and checks["launcher"]:
                try:
                    missing = self._missing_entries()
                except (BackendError, yostar.ManifestError) as exc:
                    # Offline or a broken manifest: do not call the files bad.
                    manifest_note = f" (could not compare with the game's file list: {exc})"
                else:
                    checks["files"] = not missing
        failed = [name for name, ok in checks.items() if not ok]
        if not failed:
            message = "all checks passed" + manifest_note
        elif direct and missing:
            names = ", ".join(entry.path for entry in missing[:5])
            if len(missing) > 5:
                names += f" (+{len(missing) - 5} more)"
            message = (
                f"integrity check failed: {len(missing)} game file(s) missing or changed "
                f"({names}); click INSTALL or UPDATE, or Repair files, to finish"
            )
        elif direct and checks.get("installation") is False:
            message = "integrity check failed: installation incomplete — click INSTALL to finish"
        else:
            message = "integrity check failed: " + ", ".join(failed)
        if not checks["prefix"]:
            message += " Rebuild the Wine prefix in Settings."
        return {
            "verified": not failed,
            "message": message,
            "checks": checks,
            "failed": failed,
            "prefix": str(self.prefix()),
            "exe": str(target) if target else "",
        }

    def _refuse_if_shared(self, tree: Path) -> None:
        """Never delete a tree another profile's install dir or prefix overlaps."""
        from ..core.removal import _shared_with

        other = _shared_with(self.ctx.config.profiles, self.ctx.name, tree.resolve())
        if other:
            raise BackendError(f"refusing to remove {tree}: it is shared with profile {other!r}")

    def uninstall(self, purge: bool = False) -> None:
        prefix = self.prefix()
        data_dir = self._data_dir()
        recorded = profile_setting(self.ctx.profile, INSTALLED_PREFIX_KEY)
        # ``prefix`` is user-controlled: only remove it when it is inside this
        # profile's data dir or matches the prefix recorded at install time.
        reason = _dangerous_removal_reason(
            prefix, data_dir, str(recorded) if recorded not in (None, "") else None
        )
        if reason is not None:
            raise BackendError(
                f"refusing to remove the prefix {prefix}: it is {reason} "
                "(reinstall to record it, or set 'prefix' under the profile's data dir)"
            )
        if (prefix / "drive_c").is_dir():
            self._refuse_if_shared(prefix)
            self.ctx.emit(Event("step", f"Removing the game prefix {prefix}"))
            # ``prefix`` may be a symlink, which rmtree refuses: remove the
            # resolved tree, then drop the (now dangling) link.
            shutil.rmtree(prefix.resolve())
            if prefix.is_symlink():
                prefix.unlink(missing_ok=True)
        if purge:
            self.ctx.emit(Event("step", "Purging profile data (cache, prefix)"))
            target = _purge_target(
                data_dir, str(recorded) if recorded not in (None, "") else None
            )
            if target is None:
                self.ctx.emit(
                    Event(
                        "step",
                        f"{data_dir} links to {data_dir.resolve()}, which may hold other "
                        "files: removing the link and leaving that folder in place",
                    )
                )
            else:
                self._refuse_if_shared(target)
                shutil.rmtree(target)
            if data_dir.is_symlink():
                data_dir.unlink(missing_ok=True)
        self.ctx.emit(Event("done", "Uninstalled"))


__all__ = [
    "KIND",
    "INSTALLER_API",
    "DEFAULT_INSTALLER_URL",
    "DEFAULT_INSTALL_SUBDIR",
    "INSTALL_MARKER",
    "LAUNCHER_NAMES",
    "ArknightsBackend",
]
