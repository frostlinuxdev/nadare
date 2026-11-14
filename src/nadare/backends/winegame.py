# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Wine/Proton game backend shared by the direct-download and launcher games.


This is the neutral half of the Arknights backend: prefix and runner handling,
launch-command construction, process detection, installer download/run and the
common lifecycle.  The Yostar-specific manifest machinery stays in
:mod:`nadare.backends.arknights`.
"""


from __future__ import annotations

import logging
import shutil
import time as time  # noqa: E402, F401
from pathlib import Path
from typing import Any

from ..core import (
    diskspace,
    graphics,
    paths,
    prefixmove,
    runlock,
    runners,
    runtimewatch,
    sandbox,
    umunoise,
)

# Names that tests and callers reach through this module; the code using them moved to
# winegame_parts/ (#1248). Re-exported so ``winegame.X`` is the same object.
from ..core import download as download  # noqa: E402, F401
from ..core import hostlibs as hostlibs  # noqa: E402, F401
from ..core import umu as umu  # noqa: E402, F401
from ..core import vcredist as vcredist  # noqa: E402, F401
from ..core.backends import (
    BackendContext,
    BackendError,
    BackendUnsupported,
    GameBackend,
    Status,
)
from ..core.events import Event, mask
from ..core.gameversion import versions_equal
from ..core.sandboxed import SandboxMixin
from .arknights_news import IMAGE_HOSTS
from .wine import (
    ProcessQuery,
    path_option,
    profile_setting,
    scan_pids,
    stream,
)
from .winegame_parts.installer import (
    InstallerMixin,
)
from .winegame_parts.launching import (
    _SEARCH_MAX_DEPTH,
    _SEARCH_MAX_DIRS,
    LaunchingMixin,
)
from .winegame_parts.runner import (
    RunnerMixin,
)
from .winegame_parts.shared import (
    _atomic_write_json,
)
from .winegame_parts.versions import (
    _UPDATE_LINE,
    INSTALL_MARKER,
    UPDATE_CHECK_INTERVAL,
    VERSION_FILE,
    VersionsMixin,
    _marker_version,
)

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


#: Marks "look the launcher up" for `_patterns` (``None`` already means "not installed").
_SEARCH = object()


#: Profile key recording the prefix an install actually used. Uninstall only
#: removes a prefix inside the profile data dir or one that matches this record,
#: so a crafted/imported ``prefix`` cannot delete an unrelated tree.
INSTALLED_PREFIX_KEY = "installed_prefix"


KIND = "arknights-yostar"


#: Public name for sibling backends (the layering rule keeps ``_`` names private).
atomic_write_json = _atomic_write_json


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


class WineGameBackend(
    InstallerMixin,
    LaunchingMixin,
    RunnerMixin,
    VersionsMixin,
    SandboxMixin,
    GameBackend,
):
    unofficial_notice = True
    install_size_hint = int(3.5 * 1024**3)

    #: Display driver when the profile says ``auto``: ``wayland`` or ``x11``.
    default_display = "wayland"
    #: Default ``DXVK_CONFIG``. DXVK 3.1.1's VK_EXT_present_timing path makes
    #: the client hang on its first present (issue #136); older DXVK ignores the
    #: unknown key. A profile ``extra_env`` DXVK_CONFIG overrides it.
    dxvk_config = "dxvk.enablePresentTiming = False"
    kind = KIND
    #: Run the real Microsoft VC++ installer in the prefix. Off by default.
    vcredist_default = False

    #: Game tag used for signed API calls / manifests.
    game_tag = "Arknights_EN"
    display_name = "Arknights"

    #: Default install sub-path and launcher candidates, overridable per game.
    install_subdir: Path = DEFAULT_INSTALL_SUBDIR
    launcher_names: tuple[str, ...] = LAUNCHER_NAMES
    direct_exe: str = "Arknights.exe"
    #: Installer defaults and trust, overridable by launcher-only games.
    default_installer_url: str = DEFAULT_INSTALLER_URL
    installer_api: str | None = INSTALLER_API
    #: Pin document tag for this game's installer, when the index publishes one.
    installer_pin_tag: str | None = None

    #: A non-strict runner setup failure, kept so a new prefix never falls back
    #: to a bare ``wine`` that is not installed (or not wanted).
    _runner_setup_error: Exception | None = None

    #: Official image hosts, overridden by games with their own news source.
    official_image_hosts = IMAGE_HOSTS

    def __init__(self, ctx: BackendContext) -> None:
        super().__init__(ctx)
        self._watch = runtimewatch.RuntimeWatch()

    def _run_unwrapped(self, argv: list[str], env: dict[str, str], *, on_line) -> int:  # noqa: ANN001
        """A setup step outside the Sandbox (core/sandboxed.py asks for umu's runtime first)."""
        return stream(argv, env, on_line=on_line, cancel=self.ctx.cancel)

    def _on_line(self, line: str) -> None:
        self._watch.feed(line)
        self.info(mask(line))

    def _begin_game_output(self) -> None:
        """A game run starts: umu-run's Stop traceback is folded into one line (#675)."""
        self._stop_noise = umunoise.StopNoiseFilter()

    def _on_game_line(self, line: str) -> None:
        self._watch.feed(line)
        for out in self._stop_noise.feed(line):
            self.info(mask(out))

    def _end_game_output(self) -> None:
        for out in self._stop_noise.flush():
            self.info(mask(out))

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

    def _sandbox_prefix(self) -> Path:
        return self.prefix()

    def _sandbox_game_dir(self) -> Path | None:
        return self.install_dir()

    def cache_dir(self) -> Path:
        return paths.cache_dir() / KIND

    def direct_target(self) -> Path | None:
        """The game exe a direct install/launch uses (profile override wins)."""
        raw = profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        )
        if raw:
            return path_option(raw, base=self.install_dir())
        return self.install_dir() / self.direct_exe

    def manifest_source(self) -> str:
        """The configured manifest URL/file; only the Yostar backend has one, so ``""`` here."""
        return ""

    #: Errors a manifest comparison may raise that ``verify`` and ``repair`` handle instead of
    #: letting them propagate. Only the Yostar backend has a manifest, so nothing by default.
    _manifest_errors: tuple[type[BaseException], ...] = ()

    def _manifest_error(self, exc: Exception) -> BackendError:
        """Only the Yostar backend has a manifest; direct mode never reaches this."""
        raise NotImplementedError

    def _load_manifest(self) -> Any:
        """Only the Yostar backend has a manifest; direct mode never reaches this."""
        raise NotImplementedError

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        """Only the Yostar backend has a manifest; direct mode never reaches this."""
        raise NotImplementedError

    def _running_elsewhere(self) -> bool:
        """Another Flatpak instance launched this prefix's game."""
        return sandbox.in_flatpak() and runlock.running(self.prefix())

    def _refuse_if_running(self, what: str) -> None:
        """Stop before replacing game files under a running game (#933)."""
        if scan_pids(self._patterns()) or self._running_elsewhere():
            raise BackendError(f"Stop the game before {what}", 8)

    def _sync_direct(self) -> dict[str, Any]:
        """Only the Yostar backend has a manifest; direct mode never reaches this."""
        raise NotImplementedError

    #: Environment names a launch log reports: Nadare's own plus what shapes a game's graphics.
    _LAUNCH_ENV_PREFIXES = (
        "WINE",
        "PROTON",
        "STEAM_",
        "DXVK",
        "VKD3D",
        "UMU",
        "GAMEID",
        "STORE",
        "MESA",
        "RADV",
        "__GL",
        "DRI_PRIME",
        "SDL_",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XDG_SESSION_TYPE",
        "LD_LIBRARY_PATH",
    )

    #: Whether this game offers the Sandbox (the home folder hidden while it plays).
    supports_secure_mode = False
    sandbox_default_on: bool = True

    def status(self) -> Status:
        prefix = self.prefix()
        direct = self.direct_mode()
        target = self.direct_target() if direct else self.find_launcher()
        incomplete = direct and self._install_incomplete()
        pids = scan_pids(self._patterns(target))
        elsewhere = self._running_elsewhere()
        running = bool(pids) or elsewhere
        data = {
            "prefix": str(prefix),
            "exe": str(target) if target else "",
            "runner": self.runner_id(),
            "pids": pids,
            "mode": "direct" if direct else "official",
            "can_repair": direct and self.install_dir().is_dir(),
            "renderer": getattr(self, "_renderer", None) or "",
        }
        if elsewhere:
            data["running_elsewhere"] = True
        version: str | None = None
        if direct:
            additions, version, early = self._direct_status_parts(target, incomplete)
        else:
            additions, version, early = self._official_status_parts(target)
        data.update(additions)
        if early is not None:
            early.data = data
            return early
        if self._runner_missing():
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
            state="running" if running else "installed",
            version=version,
            running=running,
            message=message,
            data=data,
        )

    def _runner_missing(self) -> bool:
        """Whether the profile's explicit runner no longer resolves (status/Verify/Repair).

        A default-family runner is downloaded on first launch, so an unset value is
        never an error here; only an explicit runner that cannot be found is.
        """
        rid = self.runner_id()
        if not rid or rid in runners.RUNNERS:
            return False
        try:
            self.resolve_runner()
        except (BackendError, runners.RunnerError):
            return True
        return False

    def _direct_status_parts(
        self, target: Path | None, incomplete: bool
    ) -> tuple[dict[str, Any], str | None, Status | None]:
        """The version/update fields and not-installed check of a direct install."""
        additions: dict[str, Any] = {}
        record = self._read_version_file()
        version = str(record["version"]) if record else None
        if version:
            additions["version"] = version
        latest = self._read_latest()
        published = str(latest["version"]) if latest else None
        # Purely local: the published version is refreshed by check_update(). Compare with the
        # same +client- rule the download plan uses, or a suffix-only change looks like an update.
        additions["update_available"] = bool(
            version and published and not versions_equal(version, published)
        )
        if additions["update_available"]:
            additions["latest_version"] = published  # the page names what Update brings
        if target is None or not target.is_file() or incomplete:
            # Files may already be on disk from a cancelled/failed install, but
            # only a finished sync counts as installed.
            interrupted = self._interrupted_update() if incomplete else None
            if interrupted is not None:
                message = (
                    f"update to {interrupted[1]} was interrupted — click RESUME to finish it; "
                    "only what is missing is downloaded"
                )
            elif incomplete:
                # Say how much is on disk, so "Cancel" is not mistaken for "deleted" (#991).
                # status() runs off the GUI thread; only an interrupted install walks the tree.
                partial = diskspace.tree_bytes(self.install_dir())
                additions["partial_bytes"] = partial
                message = (
                    f"installation incomplete — {diskspace.human_size(partial)} downloaded so "
                    "far; click RESUME to finish, or remove the game to delete it"
                )
            else:
                message = f"{self.display_name} is not installed yet"
            if incomplete:
                additions["incomplete"] = True
            return (
                additions,
                version,
                Status(state="not_installed", version=version, message=message),
            )
        return additions, version, None

    def _official_status_parts(
        self, target: Path | None
    ) -> tuple[dict[str, Any], str | None, Status | None]:
        """The not-installed check of an official launcher (no extra fields)."""
        if target is None:
            return (
                {},
                None,
                Status(
                    state="not_installed",
                    message=f"{self.display_name} is not installed yet",
                ),
            )
        return {}, None, None

    def _patterns(self, target: Path | None | object = _SEARCH) -> ProcessQuery:
        """Build the structured process query for this profile's launcher.

        Pass the launcher already resolved (``None`` for "not installed") to skip the search.
        """
        extra = profile_setting(self.ctx.profile, "process_match")
        if target is _SEARCH:
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
            self._wait_for_direct_proton(argv, env)
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
        self._direct_attempt_failed = False
        self._ensure_default_runner(strict=False)
        # Record the prefix the install uses so uninstall may later remove an
        # explicitly configured external prefix (and only that one).
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        if self.direct_mode():
            self._ensure_direct_prefix()
            self._sync_direct()
            return
        self.ctx.set("prefix", str(self.prefix()))
        if not self._ensure_prefix():
            self._run_vcredist(created=False)
        launcher_before_direct = self._launcher_state()
        if self._install_launcher_directly():
            self.ctx.emit(Event("done", self.install_done_message()))
            return
        url = self.require_installer_url(resolve=True)
        installer = self._download_installer(url)
        direct_failed = (
            self._direct_attempt_failed
            and self._launcher_state() != launcher_before_direct
        )
        # A launcher the failed direct download just wrote is not evidence for the installer.
        planted = self._launcher_state() if direct_failed else None
        had_launcher = False if direct_failed else self.find_launcher() is not None
        code = self._run_installer(installer)
        left_by_failed_attempt = (
            direct_failed and planted is not None and self._launcher_state() == planted
        )
        # A launcher left by an earlier install says nothing about this attempt (#934).
        if (
            self.find_launcher() is None
            or (had_launcher and code != 0)
            or left_by_failed_attempt
        ):
            self.info(f"{self.display_name} installer exited with code {code} and left no launcher")
            if getattr(self, "_installer_sandboxed", False):
                # Inside the Sandbox the home folder is an empty stand-in: an installer that
                # installed into S: or the home wrote into it, and that is gone now.
                raise BackendError(
                    self._watch.explain(
                        f"The {self.display_name} installer left no launcher. It ran in the "
                        "Sandbox, which hides your home folder: if it offered a folder on S: or "
                        "in your home, run Install again and choose a folder on C: (or turn "
                        "Sandbox off for this game)."
                    )
                )
            raise BackendError(
                self._watch.explain(
                    f"The {self.display_name} installer did not finish. Try Install again. "
                    "If it still "
                    "fails, open Settings → Runner and choose Proton-CachyOS."
                )
            )
        self.ctx.emit(Event("done", self.install_done_message()))

    def install_done_message(self) -> str:
        """The last line of a successful launcher install (a subclass may say what is next)."""
        return f"{self.display_name} installer finished"

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
        self.ctx.emit(
            Event("step", f"Starting the {self.display_name} launcher (it checks for updates)")
        )
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
            missing = self._missing_entries(verify=False)
        except (BackendError, *self._manifest_errors) as exc:
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
        if scan_pids(self._patterns()) or self._running_elsewhere():
            raise BackendError("Stop the game before rebuilding its Wine prefix", 8)
        # The rebuild restores only this game's folder, so a game of another profile in the
        # same prefix would be left in the backup (#919).
        self._refuse_if_shared(self.prefix(), "rebuild")
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
        backups = self.ctx.profile.get("prefix_backups")
        if not isinstance(backups, list):
            backups = []
        self.ctx.set("prefix_backups", [*backups, str(backup.resolve())])
        self.ctx.emit(
            Event(
                "done",
                f"The Wine prefix was rebuilt. The old one was kept at {backup}; "
                "delete it when you are sure everything works.",
            )
        )

    #: A launch that ends sooner than this with a runtime failure in its output never ran.
    _QUICK_EXIT_SECONDS = 30

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
        if self.runner_id():
            # Only an explicit runner can go missing (the default one is fetched at launch).
            checks["runner"] = not self._runner_missing()
        missing: list[Any] = []
        manifest_note = ""
        if direct:
            checks["installation"] = not self._install_incomplete()
            if checks["installation"] and checks["launcher"]:
                try:
                    missing = self._missing_entries()
                except (BackendError, *self._manifest_errors) as exc:
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

    def _refuse_if_shared(self, tree: Path, action: str = "remove") -> None:
        """Never delete or move a tree another profile's install dir or prefix overlaps."""
        from ..core.removal import _shared_with

        other = _shared_with(self.ctx.config.profiles, self.ctx.name, tree.resolve())
        if other:
            raise BackendError(f"refusing to {action} {tree}: it is shared with profile {other!r}")

    def _remove_tree_safely(self, tree: Path) -> None:
        """Delete ``tree`` without entering a mounted drive; report what was left (#947)."""
        from ..core import removal

        outcome = removal._rmtree_safely(tree)
        left = [f"{m} is a separate mounted drive, left in place" for m in outcome.mounts]
        left += [removal._leftover_warning(p, e) for p, e in outcome.leftovers]
        if left:
            raise BackendError(
                f"{tree} was only partly removed ({'; '.join(left)}). "
                "Everything else in it is gone; the profile is kept."
            )

    def uninstall(self, purge: bool = False) -> None:
        if scan_pids(self._patterns()) or self._running_elsewhere():
            raise BackendError("Stop the game before uninstalling it", 8)
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
            if prefix.is_symlink():
                # The link may have been moved to another disk or swapped after
                # install, so its target can hold unrelated data: drop the link
                # and leave the folder it points at.
                self.ctx.emit(
                    Event(
                        "step",
                        f"{prefix} links to {prefix.resolve()}, which may hold other "
                        "files: removing the link and leaving that folder in place",
                    )
                )
                prefix.unlink(missing_ok=True)
            else:
                self._refuse_if_shared(prefix)
                self.ctx.emit(Event("step", f"Removing the game prefix {prefix}"))
                self._remove_tree_safely(prefix)
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
                self._remove_tree_safely(target)
            if data_dir.is_symlink():
                data_dir.unlink(missing_ok=True)
        self.ctx.emit(Event("done", "Uninstalled"))


__all__ = [
    "KIND",
    "WineGameBackend",
    "_SEARCH_MAX_DEPTH",
    "_SEARCH_MAX_DIRS",
    "INSTALL_MARKER",
    "_UPDATE_LINE",
    "_marker_version",
    "VERSION_FILE",
    "UPDATE_CHECK_INTERVAL",
    "_atomic_write_json",
]
