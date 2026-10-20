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

import shlex
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from ..core import diskspace, download, graphics, paths, process, runners, sandbox, vcredist, yostar
from ..core.backends import BackendError, BackendUnsupported, GameBackend, Option, Status
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


def _purge_target(data_dir: Path, recorded_prefix: str | None = None) -> Path:
    """The resolved directory a ``purge`` may remove, or raise ``BackendError``.

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
        if prefix_resolved is not None and (
            prefix_resolved == resolved or resolved in prefix_resolved.parents
        ):
            return resolved
    raise BackendError(f"refusing to purge {data_dir}: it resolves outside the profile directory")


class ArknightsBackend(GameBackend):
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

    #: Game tag used for signed Yostar API calls / manifests.
    game_tag = "Arknights_EN"
    display_name = "Arknights"

    #: Default install sub-path and launcher candidates, overridable so the
    #: Stella Sora backend can reuse the same Wine/runner machinery.
    install_subdir: Path = DEFAULT_INSTALL_SUBDIR
    launcher_names: tuple[str, ...] = LAUNCHER_NAMES
    direct_exe: str = "Arknights.exe"

    #: Official Arknights Global news (Yostar's own public API); images come
    #: from the publisher's CDN only.
    news_source = staticmethod(fetch_arknights_news)
    official_image_hosts = IMAGE_HOSTS

    #: The CI media document supplies the launcher hero + operation banners.
    official_media = True

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
        return str(raw).strip() if raw else ""

    def resolve_runner(self) -> tuple[str | None, str]:
        """Return ``(path, kind)`` with kind ``proton`` or ``wine``."""
        rid = self.runner_id()
        if not rid or rid.lower() in ("wine", "system", "none"):
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
        return DEFAULT_INSTALLER_URL

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
        return yostar.base_url_trusted(url)

    def require_installer_url(self, *, resolve: bool = False) -> str:
        url = self.installer_url(resolve=resolve)
        if not self._installer_is_usable(url):
            raise BackendError(
                "no usable Arknights installer URL: set profile 'installer_url' to the "
                "official Windows client .exe link on a Yostar host (or pin its "
                "'installer_sha256'); the pinned default may be stale (see TODO)"
            )
        return url

    def _fetch_installer_url(self, *, timeout: int = 30) -> str | None:
        try:
            payload = download.fetch_json(INSTALLER_API, timeout=timeout)
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
        redirect_allowed = None if pinned else yostar.base_url_trusted
        size = download.remote_size(url, redirect_allowed=redirect_allowed)
        if size:
            diskspace.check_space(dest.parent, size)
        self.ctx.emit(Event("step", f"Downloading the Arknights PC client installer ({dest.name})"))
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

    def _load_manifest(self) -> yostar.Manifest:
        try:
            return yostar.load_manifest(self.manifest_source(), game_tag=self.game_tag)
        except yostar.ManifestError as exc:
            raise BackendError(str(exc)) from exc

    def _sync_direct(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        workers = profile_setting(self.ctx.profile, "download_workers", 6)
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
            raise BackendError(str(exc)) from exc
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

    def _launch_direct(self) -> int:
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._run_vcredist(created=False)
        argv, env = self._prefix_command([str(target), *self._extra_args("launch_args")])
        self.ctx.emit(Event("step", f"{self.display_name}: launching {target.name}"))
        self.info(self._runtime_note())
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        _renderer, note = graphics.effective_renderer(self.ctx.profile)
        if note:
            self.info(note)
        probe = self._renderer_probe().start()
        try:
            return stream(
                argv, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel
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
            raise BackendError(f"Arknights launcher not found under {self.install_dir()}")
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
            umu = shutil.which(str(profile_setting(self.ctx.profile, "umu_binary", "umu-run")))
            env["PROTONPATH"] = str(proton)
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(prefix)
            env.setdefault("PROTON_ENABLE_WAYLAND", self._wayland_default())
            if umu:
                argv = [umu, *args]
            elif sandbox.in_flatpak():
                argv, env_updates = sandbox.proton_direct(proton, prefix, args)
                env.update(env_updates)
            else:
                raise BackendError("the selected runner needs umu-run on PATH")
        else:
            wine = profile_setting(self.ctx.profile, "wine_binary")
            resolved_wine = path_option(wine)
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
        return stream(cmd, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel)

    def _runtime_available(self) -> bool:
        """Whether a Wine/Proton runtime is explicitly configured for direct mode.

        Only an explicitly configured runner or Wine binary counts: a backend
        test that sets neither must never fall back to a real ``wine`` on PATH.
        """
        if self.runner_id():
            return True
        return bool(profile_setting(self.ctx.profile, "wine_binary"))

    def _run_vcredist(self, *, created: bool = False) -> None:
        """Best-effort VC++ runtime step (see :func:`nadare.core.vcredist.ensure`).

        Runs on a newly created prefix, or on an existing one only when the
        profile has ``"vcredist": true``. ``"vcredist": false`` disables it
        everywhere.
        """
        setting = profile_setting(self.ctx.profile, "vcredist")
        if setting is False:
            return
        if not created and setting is not True:
            return
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
        target = self.find_launcher()
        pids = scan_pids(self._patterns(target))
        data = {
            "prefix": str(prefix),
            "exe": str(target) if target else "",
            "runner": self.runner_id(),
            "pids": pids,
            "mode": "direct" if self.direct_mode() else "official",
            "renderer": getattr(self, "_renderer", None) or "",
        }
        if target is None:
            return Status(
                state="not_installed",
                message=f"{self.display_name} not found",
                data=data,
            )
        message = ""
        note = graphics.vulkan_note()
        if note:
            message = note
        return Status(
            state="running" if pids else "installed",
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

    def _ensure_prefix(self) -> None:
        prefix = self.prefix()
        if (prefix / "drive_c").is_dir():
            return
        prefix.mkdir(parents=True, exist_ok=True)
        self.ctx.emit(Event("step", f"Creating the Wine prefix at {prefix}"))
        argv, env = self._prefix_command(["wineboot", "-u"])
        code = stream(
            argv, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel
        )
        if code != 0 or not (prefix / "drive_c").is_dir():
            raise BackendError(f"prefix creation failed (exit {code})")
        self._run_vcredist(created=True)

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        # Record the prefix the install uses so uninstall may later remove an
        # explicitly configured external prefix (and only that one).
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        if self.direct_mode():
            self._ensure_direct_prefix()
            self._sync_direct()
            return
        url = self.require_installer_url(resolve=True)
        self.ctx.set("prefix", str(self.prefix()))
        self._ensure_prefix()
        self._run_vcredist(created=False)
        installer = self._download_installer(url)
        argv, env = self._prefix_command([str(installer), *self._extra_args("installer_args")])
        self.ctx.emit(Event("step", f"Running the Arknights installer in {self.prefix()}"))
        self.ctx.emit(
            Event(
                "step",
                "Finish the installer window; the Arknights launcher will then download the game",
            )
        )
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        code = stream(
            argv, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel
        )
        if self.find_launcher() is None:
            raise BackendError(
                f"the Arknights launcher was not installed (installer exit code {code}). "
                "If the installer showed a network error, try the Proton-CachyOS "
                "runner, or use direct download."
            )
        self.ctx.emit(Event("done", "Arknights installer finished"))

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
        self.ctx.emit(Event("step", "Starting the Arknights launcher (it checks for updates)"))
        code = self.launch()
        if code != 0:
            raise BackendError(f"the Arknights launcher exited with code {code}")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        if self.direct_mode():
            return self._launch_direct()
        self._run_vcredist(created=False)
        argv, env, target = self.build_launch()
        self.ctx.emit(Event("step", f"Arknights: launching {target.name}"))
        self.info(self._runtime_note())
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        _renderer, note = graphics.effective_renderer(self.ctx.profile)
        if note:
            self.info(note)
        probe = self._renderer_probe().start()
        try:
            return stream(
                argv, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel
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
        target = self.find_launcher()
        checks = {
            "prefix": (self.prefix() / "drive_c").is_dir(),
            "launcher": target is not None,
        }
        failed = [name for name, ok in checks.items() if not ok]
        message = (
            "all checks passed" if not failed else "integrity check failed: " + ", ".join(failed)
        )
        return {
            "verified": not failed,
            "message": message,
            "checks": checks,
            "failed": failed,
            "prefix": str(self.prefix()),
            "exe": str(target) if target else "",
        }

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
            shutil.rmtree(target)
            if data_dir.is_symlink():
                data_dir.unlink(missing_ok=True)
        self.ctx.emit(Event("done", "Uninstalled"))


__all__ = [
    "KIND",
    "INSTALLER_API",
    "DEFAULT_INSTALLER_URL",
    "DEFAULT_INSTALL_SUBDIR",
    "LAUNCHER_NAMES",
    "ArknightsBackend",
]
