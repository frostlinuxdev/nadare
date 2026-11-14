# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generic Wine/Proton backend.

Adapted from the stella-launcher-client ``launcher.py`` runner discovery and
launch-mode table, trimmed to the GameBackend contract: build the command,
fork it with output streamed as Events, detect and stop a running game.

Profile keys (top level or under ``extra``):

``install_dir`` / ``exe_name`` (or ``launch_exe``)
    the Windows executable to fork.
``prefix`` / ``wineprefix``
    ``WINEPREFIX`` (defaults to the profile data dir's ``prefix``).
``proton_runner`` (or ``runner``)
    a Steam Linux Runtime Proton runner directory; when set the game runs
    through ``umu-run`` with ``PROTONPATH`` instead of a bare Wine.
``wine_binary``
    Wine executable. When empty, the game runs on the default Proton runner.
``umu_binary``
    ``umu-run`` (default).
``launch_args``
    extra argv / a shell-word string appended after the executable.
``extra_env``
    mapping of extra environment variables.
``process_match``
    extra substring used to detect/stop the running game.

The process helpers here (``stream``/``scan_pids``/``stop_pids``) are shared
with the other generic backends, which use the same subprocess machinery.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import (
    download,
    graphics,
    legacywine,
    monitors,
    paths,
    process,
    runners,
    sandbox,
    umu,
    userns,
    vcredist,
)
from ..core.backends import PREFIX_HELP, BackendError, GameBackend, Option, Status
from ..core.config import as_bool, profile_value
from ..core.events import Cancel, Cancelled, Event, mask
from ..core.sandboxed import SandboxMixin

_SIGNAL_TERM = 15


# --------------------------------------------------------------------------
# shared helpers (imported by steam.py / script.py)
# --------------------------------------------------------------------------
def profile_setting(profile: dict[str, Any], key: str, default: Any = None) -> Any:
    """Read a profile key from the top level or from its ``extra`` mapping."""
    return profile_value(profile, key, default)


def path_option(value: Any, base: Path | None = None) -> Path | None:
    raw = str(value).strip() if value not in (None, "") else ""
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() and base is not None:
        path = Path(base) / path
    return path


def _wineserver_for(argv: Iterable[str], env: dict[str, str]) -> tuple[Path, Path | None] | None:
    """Return ``(wineserver, prefix)`` for the Wine/Proton runner, if known.

    For a Proton runner (``PROTONPATH`` set) the wineserver lives inside the
    runner tree; for bare Wine it sits next to the wine binary. Only the runner
    that was actually started is used, never an unrelated wineserver on PATH.
    """
    cmd = [str(a) for a in argv]
    if not cmd:
        return None
    env = env or {}
    proton = env.get("PROTONPATH")
    if proton:
        wineserver = Path(proton) / "files" / "bin" / "wineserver"
    else:
        wineserver = Path(cmd[0]).parent / "wineserver"
    # umu uses an explicitly-set WINEPREFIX as the Wine prefix (and mirrors it
    # into STEAM_COMPAT_DATA_PATH), so prefer that when present.
    prefix_raw = env.get("WINEPREFIX") or env.get("STEAM_COMPAT_DATA_PATH")
    prefix = Path(prefix_raw) if prefix_raw else None
    return wineserver, prefix


#: A cancelled launch and the Stop queued behind it would otherwise run the same
#: ``wineserver -k`` twice (2 x 15 s). An immediate repeat after a successful kill is skipped; a
#: failed kill, or a later Stop, retries.
_WINESERVER_KILLED: dict[str, float] = {}
_WINESERVER_KILL_COOLDOWN = 3.0


def _kill_key(prefix: Path) -> str:
    """The prefix's real location, so one wineserver is one entry however it was spelled."""
    try:
        return os.path.realpath(str(prefix))
    except (OSError, ValueError):
        return os.path.normpath(str(prefix))


def _forget_kill(argv: Iterable[str], env: dict[str, str]) -> None:
    """A new process run means a new wineserver: an earlier kill record is about an old one."""
    found = _wineserver_for(argv, env)
    if found is not None and found[1] is not None:
        _WINESERVER_KILLED.pop(_kill_key(found[1]), None)


def _stop_wineserver(argv: Iterable[str], env: dict[str, str]) -> None:
    """Best-effort ``wineserver -k`` for the runner behind ``argv``.

    Called only after a cancelled operation; a normal child exit leaves the
    wineserver alone. A missing wineserver is a no-op (never falls back to an
    unrelated ``wineserver`` on PATH).
    """
    found = _wineserver_for(argv, env)
    if found is None:
        return
    wineserver, prefix = found
    if prefix is None:
        return  # no owned prefix: never kill the ambient/default wineserver
    if not wineserver.is_file():
        return
    key = _kill_key(prefix)
    now = time.monotonic()
    if now - _WINESERVER_KILLED.get(key, -_WINESERVER_KILL_COOLDOWN) < _WINESERVER_KILL_COOLDOWN:
        return
    child_env = dict(env)
    child_env["WINEPREFIX"] = str(prefix)
    try:
        subprocess.run(
            [str(wineserver), "-k"],
            env=child_env,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return  # it may still be alive: a retry must not be skipped
    _WINESERVER_KILLED[key] = time.monotonic()


def stop_runner_wineserver(
    *, prefix: Path, proton: str | None = None, wine_binary: Path | None = None
) -> None:
    """Best-effort ``wineserver -k`` for the runner a profile actually uses.

    Resolves the wineserver the same way a launch does (runner-local for
    Proton, next to the wine binary for bare Wine) instead of the first
    ``wineserver`` on ``PATH``. Uses Nadare's pinned XDG environment so it
    never touches the host's real ``~/.local``.
    """
    env = process.build_env(None, extra={"WINEPREFIX": str(prefix)})
    if proton:
        env["PROTONPATH"] = str(proton)
        argv = [str(proton)]
    elif wine_binary is not None:
        argv = [str(wine_binary)]
    else:
        try:
            argv = [str(first_wine())]
        except BackendError:
            return
    _stop_wineserver(argv, env)


def stream(
    argv: Iterable[str],
    env: dict[str, str],
    on_line: Callable[[str], None] | None = None,
    cancel: Cancel | None = None,
    stop_wine: bool = True,
    cwd: str | os.PathLike[str] | None = None,
) -> int:
    """Run ``argv`` in its own process group, streaming merged output.

    Cancellation is checked at least every 0.2 s regardless of output, the
    whole process group is signalled, and the call returns as soon as the
    direct child exits even if a grandchild (wineserver) holds the stdout pipe.
    """
    cmd = [str(part) for part in argv]
    emit = None
    if on_line is not None:

        def emit(event: Event) -> None:
            if event.kind == "log":
                on_line(event.message)

    _forget_kill(cmd, env)
    try:
        completed = process.run(
            cmd, env=env, emit=emit, cancel=cancel, errors="replace", cwd=cwd
        )
    except Cancelled:
        if stop_wine:
            _stop_wineserver(cmd, env)
        raise
    except OSError as exc:
        raise BackendError(f"could not start {cmd[0]}: {exc}") from exc
    return completed.returncode


@dataclass(frozen=True)
class ProcessQuery:
    """A structured process match.

    Only a process that actually belongs to the game is counted:

    * its ``WINEPREFIX`` / ``STEAM_COMPAT_DATA_PATH`` equals :attr:`prefix`
      (the Wine/Proton/umu process tree), or
    * it has *no* Wine prefix environment and its ``argv[0]`` basename matches
      one of :attr:`names` (the game or launcher executable, e.g. ``game.exe``
      started directly), or
    * one of its argv elements equals one of :attr:`args` exactly (a resolved
      script path, a ``steam://rungameid/<id>`` token).

    Matching a bare substring of a path is *never* enough: a shell, editor or
    file manager that merely mentions the prefix must not read as "running".
    A process in a *different* Wine prefix is never counted, even when its exe
    name coincides. ``match`` is an explicit user override and is still matched
    as a substring of the full command line only when set.
    """

    prefix: str = ""
    names: tuple[str, ...] = ()
    match: str = ""
    args: tuple[str, ...] = ()
    #: When set, an :attr:`args` hit only counts if ``argv[0]``'s basename is one of these
    #: (an editor that has the script open must not read as the script running).
    arg_runners: tuple[str, ...] = ()
    _extra: tuple[str, ...] = field(default=(), repr=False)


#: Backwards-compatible alias.
ProcessPatterns = ProcessQuery


def cmdline_argv(entry: Path) -> list[str] | None:
    """Split ``/proc/<pid>/cmdline`` into argv, or ``None`` when unreadable."""
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _environ_map(entry: Path) -> dict[str, str]:
    """Parse ``/proc/<pid>/environ`` into a mapping (best effort)."""
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        key, _, value = chunk.partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _same_path(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    if os.path.normpath(first) == os.path.normpath(second):
        return True
    # A game in the Flatpak Sandbox is told the real path of a symlinked game folder
    # (~/Games -> another drive), while the profile names the symlink.
    return os.path.realpath(first) == os.path.realpath(second)


def _is_prefix(value: str, prefix: str) -> bool:
    """``value`` is the prefix itself or the ``pfx`` folder Proton keeps inside it."""
    return _same_path(value, prefix) or _same_path(value, os.path.join(prefix, "pfx"))


def _query_matches(query: ProcessQuery, argv: list[str], env: dict[str, str]) -> bool:
    prefix = str(query.prefix).strip() if query.prefix else ""
    names = {name.lower() for name in query.names if name}
    extra = str(query.match).strip() if query.match else ""
    wanted_args = {arg for arg in query.args if arg}
    # Proton and umu run Wine in <prefix>/pfx and say so in WINEPREFIX, while
    # STEAM_COMPAT_DATA_PATH is the prefix itself: either one names the game's prefix.
    env_prefixes = [
        value for value in (env.get("WINEPREFIX"), env.get("STEAM_COMPAT_DATA_PATH")) if value
    ]
    belongs = bool(prefix) and any(_is_prefix(value, prefix) for value in env_prefixes)
    if not belongs and names and not env_prefixes:
        basename = os.path.basename(argv[0]).lower()
        belongs = basename in names
    if not belongs and wanted_args:
        if query.arg_runners:
            runner = os.path.basename(argv[0]).lower() if argv else ""
            if runner in {name.lower() for name in query.arg_runners}:
                belongs = any(arg in argv[1:] for arg in wanted_args)
        else:
            belongs = any(arg in argv for arg in wanted_args)
    if not belongs and extra:
        belongs = extra in " ".join(argv)
    return belongs


def _own_pid() -> int:
    """This process's PID, which a scan never reports (tests point it away from fake PIDs)."""
    return os.getpid()


def _scan_query(query: ProcessQuery, *, proc_root: str | os.PathLike[str] = "/proc") -> list[int]:
    """Return PIDs that belong to ``query`` under ``proc_root``."""
    prefix = str(query.prefix).strip() if query.prefix else ""
    names = {name.lower() for name in query.names if name}
    extra = str(query.match).strip() if query.match else ""
    wanted_args = {arg for arg in query.args if arg}
    if not prefix and not names and not extra and not wanted_args:
        return []
    me = _own_pid()
    uid = os.getuid()
    hits: list[int] = []
    proc = Path(proc_root)
    try:
        entries = list(proc.iterdir())
    except OSError:
        return hits
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            # Another user's environ is unreadable, which would make the
            # basename/argument branches match a process we cannot stop.
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = cmdline_argv(entry)
        if not argv:
            continue
        # The environment only matters to the prefix and basename branches below.
        env = _environ_map(entry) if (prefix or names) else {}
        if _query_matches(query, argv, env):
            hits.append(pid)
    return hits


#: pid -> start time captured at scan, so ``stop_pids`` can tell a recycled PID.
_SCAN_START: dict[int, str | None] = {}


#: Status probes scan every few seconds; keep only the newest records.
_SCAN_START_MAX = 4096


def remember_start(pids: list[int]) -> list[int]:
    for pid in pids:
        _SCAN_START.pop(pid, None)  # re-insert: newest last
        _SCAN_START[pid] = process._pid_starttime(pid)
    while len(_SCAN_START) > _SCAN_START_MAX:
        del _SCAN_START[next(iter(_SCAN_START))]
    return pids


def scan_pids(
    patterns: ProcessQuery | Iterable[str],
    names: Iterable[str] | None = None,
    *,
    proc_root: str | os.PathLike[str] = "/proc",
    process_match: str = "",
) -> list[int]:
    """PIDs matching a :class:`ProcessQuery` (or legacy substring patterns).

    Accepts either a structured :class:`ProcessQuery`, a ``(prefix, names)``
    pair, or the older iterable of substring patterns.  The legacy iterable
    branch keeps the generic backends (Steam app id, script name) working
    unchanged; the Wine/Proton backends pass a structured query so they never
    match a bare path.
    """
    if isinstance(patterns, ProcessQuery):
        round_ = process.current_process_scan_round()
        if round_ is not None and Path(proc_root) == round_.proc_root:
            hits: list[int] = []
            for record in round_.records():
                if _query_matches(patterns, record.argv, record.environ):
                    _SCAN_START[record.pid] = record.started
                    hits.append(record.pid)
            return hits
        return remember_start(_scan_query(patterns, proc_root=proc_root))
    if names is not None:
        query = ProcessQuery(
            prefix=str(patterns), names=tuple(str(n) for n in names), match=process_match
        )
        return remember_start(_scan_query(query, proc_root=proc_root))
    wanted = [p for p in patterns if p]
    if not wanted:
        return []
    me = _own_pid()
    hits: list[int] = []
    proc = Path(proc_root)
    try:
        entries = list(proc.iterdir())
    except OSError:
        return hits
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if any(pattern in cmd for pattern in wanted):
            hits.append(pid)
    return remember_start(hits)


def stop_pids(pids: Iterable[int], signal_: int = _SIGNAL_TERM) -> None:
    """Signal ``pids`` that are still the processes a scan saw.

    A PID whose start time was not recorded (it was unreadable at the scan, usually because the
    process had just ended) is never signalled: its number may belong to anything by now. The
    record is kept, so a second Stop checks again instead of signalling blind.
    """
    for pid in pids:
        started = _SCAN_START.get(pid)
        if started is None or process._pid_starttime(pid) != started:
            continue  # gone, recycled onto an unrelated process, or never identified
        try:
            os.kill(pid, signal_)
        except OSError:
            continue


def first_wine() -> Path:
    """The first plain Wine found, for callers that still need one."""
    found = legacywine.find_plain_wine()
    if found is not None:
        return found
    # Keep the module-level lookup hook for callers that patch it in tests.
    found = shutil.which("wine")
    if found:
        return Path(found)
    raise BackendError("wine not found on PATH (set wine_binary in the profile)")


# --------------------------------------------------------------------------
class WineBackend(SandboxMixin, GameBackend):
    kind = "wine"
    supports_update = False  # the game or its own launcher updates itself
    supports_secure_mode = True
    sandbox_default_on = False

    options = (
        Option("install_dir", "Game folder", "path_dir", "Where the game lives."),
        Option(
            "exe_name",
            "Executable",
            "text",
            "Windows executable, relative to the game folder.",
        ),
        Option("prefix", "Wine prefix", "path_dir", PREFIX_HELP),
        Option("runner", "Runner", "runner", "Proton build/tag; empty uses the default Proton."),
        Option(
            "renderer",
            "Renderer",
            "choice",
            graphics.RENDERER_HELP,
            default="auto",
            choices=graphics.RENDERER_CHOICES,
        ),
        Option("launch_args", "Launch arguments", "args", "Extra arguments after the executable."),
        Option(
            "secure_mode",
            "Sandbox",
            "bool",
            "Hides your home folder from the game (needs bubblewrap). The game can still use "
            "the network. Off by default for a game you added yourself: turn it on if the "
            "game needs nothing outside its folder. See docs/SECURE-MODE.md.",
            default=False,
        ),
        Option(
            "wine_binary",
            "Wine binary",
            "path_file",
            "A custom Wine executable; empty uses the default Proton.",
            advanced=True,
        ),
        Option(
            "umu_binary",
            "umu-run binary",
            "path_file",
            "umu-run executable used with a Proton runner.",
            advanced=True,
        ),
        Option(
            "extra_env",
            "Environment",
            "env",
            "One KEY=VALUE per line.",
            advanced=True,
        ),
        monitors.OPTION,
        Option(
            "process_match",
            "Process match",
            "text",
            "Extra substring used to detect the running game.",
            advanced=True,
        ),
        Option(
            "vcredist",
            "Visual C++ runtime",
            "bool",
            "Install the Microsoft Visual C++ runtime into the prefix (recommended; "
            "fixes stutter in some games). Off by default for an existing prefix; "
            "this backend never creates a prefix.",
            default=False,
        ),
    )

    # ---- profile resolution -----------------------------------------------
    def _install_dir(self) -> Path:
        configured = path_option(profile_setting(self.ctx.profile, "install_dir"))
        # Same default as the script backend, so status() and launch() agree.
        return configured if configured is not None else self.ctx.data_dir()

    def _prefix(self) -> Path:
        configured = path_option(
            profile_setting(self.ctx.profile, "prefix")
            or profile_setting(self.ctx.profile, "wineprefix")
        )
        return configured if configured is not None else self.ctx.data_dir() / "prefix"

    def _target(self) -> Path:
        raw = (
            profile_setting(self.ctx.profile, "exe_name")
            or profile_setting(self.ctx.profile, "launch_exe")
            or profile_setting(self.ctx.profile, "script")
        )
        if not raw:
            raise BackendError(
                "No game executable is set for this profile. Open Settings → Game "
                "options and choose the .exe."
            )
        target = path_option(raw, base=self._install_dir())
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        return target

    def _sandbox_prefix(self) -> Path:
        return self._prefix()

    def _sandbox_game_dir(self) -> Path | None:
        install_dir = self._install_dir()
        try:
            target = self._target().resolve()
        except BackendError:
            return install_dir
        install_dir = install_dir.resolve()
        return install_dir if target.is_relative_to(install_dir) else target.parent

    def _wine_binary(self) -> Path | None:
        return path_option(profile_setting(self.ctx.profile, "wine_binary"))

    def _runner(self) -> str | None:
        """The Proton runner to use: the profile's choice, or the default family.

        A ``wine_binary`` always means that Wine. Otherwise an empty runner means
        the recommended Proton, never an auto-detected system Wine.
        """
        raw = profile_setting(self.ctx.profile, "proton_runner") or profile_setting(
            self.ctx.profile, "runner"
        )
        chosen = str(raw).strip() if raw else ""
        if chosen.lower() in ("wine", "system", "none"):
            chosen = ""
        if chosen:
            return chosen
        if self._wine_binary() is not None:
            return None
        return runners.DEFAULT_FAMILY

    def _runner_path(self, runner: str) -> Path:
        """Resolve a runner tag/path the same way the Arknights backend does."""
        try:
            resolved = runners.resolve_runner(runner)
        except runners.RunnerError as exc:
            raise BackendError(str(exc)) from exc
        if resolved is None:
            raise BackendError(f"runner {runner!r} not found")
        return Path(resolved)

    def _runner_for_build(self, runner: str) -> Path:
        """A family id gets its pinned build (downloading it once); anything else resolves."""
        if runner not in runners.RUNNERS:
            return self._runner_path(runner)
        try:
            return runners.ensure_pinned(
                runner, emit=self.ctx.emit, cancel=self.ctx.cancel
            )
        except (runners.RunnerError, download.DownloadError, OSError) as exc:
            raise BackendError(f"could not set up the Proton runner: {exc}") from exc

    def _runtime_error(self) -> str | None:
        """Why no launch runtime resolves, or ``None`` when one does.

        Mirrors :meth:`_prefix_command` (the launch path) without downloading
        or creating anything: a default runner that is not installed yet is
        fetched at the first launch, so status must not call it an error.
        """
        runner = self._runner()
        if runner:
            if runner in runners.RUNNERS:
                if runners.installed_pinned(runner) is None:
                    return None  # fetched at the first launch
            else:
                try:
                    self._runner_path(runner)
                except BackendError as exc:
                    return str(exc)
            # No system or managed umu yet is not an error: the launch installs Nadare's own
            # (from the AppImage's bundled archive, else a download). Status never sets it up.
            return None
        return None

    def _patterns(self, target: Path | None = None) -> ProcessQuery:
        """Build the structured process query for this profile's game."""
        extra = profile_setting(self.ctx.profile, "process_match")
        if target is None:
            try:
                target = self._target()
            except BackendError:
                target = None
        names = (target.name,) if target is not None else ()
        return ProcessQuery(
            prefix=str(self._prefix()),
            names=names,
            match=str(extra) if extra else "",
        )

    def _prefix_command(
        self, args: list[str], *, sandboxed: bool = False
    ) -> tuple[list[str], dict[str, str]]:
        """Build the Wine/``umu-run`` command and environment for ``args``.

        ``sandboxed``: the command will run in the Sandbox. Inside the Flatpak that means
        ``flatpak-spawn --sandbox``, where umu (in the app's data folder, and needing the session
        bus) cannot run, so Proton runs directly like the catalog games (VMs, 2026-11-14).
        """
        prefix = self._prefix()
        env = process.build_env(None, extra={"WINEPREFIX": str(prefix)})
        runner = self._runner()
        if runner:
            runner_path = self._runner_for_build(runner)
            env["PROTONPATH"] = str(runner_path)
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
            # Native Wayland keeps Wine in charge of its own child windows so
            # launcher dialogs/updaters behave (see the stella launcher notes).
            env.setdefault("PROTON_ENABLE_WAYLAND", "1")
            configured = str(profile_setting(self.ctx.profile, "umu_binary", "umu-run"))
            # An installed managed copy counts as it does in status(), complete host libraries
            # or not; only a download waits for missing ones (#973).
            # Every Proton launch goes through umu (user, #1338): a system umu-run, else Nadare's
            # own (installed from the AppImage's bundled archive, else downloaded once). The one
            # exception is the Flatpak's Sandbox (see the docstring).
            umu_run = (
                None
                if sandboxed and sandbox.in_flatpak()
                else umu.resolve(
                    configured,
                    allow_download=True,
                    emit=self.ctx.emit,
                    cancel=self.ctx.cancel,
                )
            )
            if umu_run:
                if not sandbox.in_flatpak() and userns.blocks_runtime():
                    raise BackendError(
                        userns.with_hint(userns.BLOCKED_BEFORE_LAUNCH, blocked=True)
                    )
                umu.note_first_runtime(self.ctx.emit)
                argv = [umu_run, *args]
            elif sandbox.in_flatpak():
                argv, env_updates = sandbox.proton_direct(runner_path, prefix, args)
                env.update(env_updates)
            else:
                message = umu.unavailable_message()
                if userns.blocks_runtime():
                    message = userns.with_hint(message, blocked=True)
                raise BackendError(message)
        else:
            resolved_wine = self._wine_binary()
            argv = [
                str(resolved_wine) if resolved_wine is not None else str(first_wine()),
                *args,
            ]
            # Plain Wine would otherwise ask to download wine-mono / wine-gecko.
            overrides = [p for p in str(env.get("WINEDLLOVERRIDES") or "").split(";") if p.strip()]
            if "mscoree,mshtml=" not in overrides:
                overrides.append("mscoree,mshtml=")
            env["WINEDLLOVERRIDES"] = ";".join(overrides)
        kind = "proton" if runner else "wine"
        env.update(graphics.renderer_env(self.ctx.profile, kind=kind, environ=env))
        monitors.apply_env(env, self.ctx)
        extra_env = profile_setting(self.ctx.profile, "extra_env")
        process.apply_extra_env(
            env,
            extra_env if isinstance(extra_env, dict) else None,
            reserved=process.LAUNCH_RESERVED_ENV,
        )
        return argv, process.sanitize_game_env(env)

    def build(self) -> tuple[list[str], dict[str, str], Path, Path]:
        """Return ``(argv, env, prefix, target)`` for the profile's launch."""
        args: list[str] = []
        extra_args = profile_setting(self.ctx.profile, "launch_args")
        if isinstance(extra_args, str) and extra_args.strip():
            args += shlex.split(extra_args)
        elif isinstance(extra_args, (list, tuple)):
            args += [str(a) for a in extra_args]
        target = self._target()
        argv, env = self._prefix_command(
            [str(target), *args], sandboxed=self.secure_mode_enabled()
        )
        argv, env = self._secure_wrap(argv, env)
        return argv, env, self._prefix(), target

    # ---- Visual C++ runtime (shared in nadare.core.vcredist) ----------------
    def _run_unwrapped(self, argv: list[str], env: dict[str, str], *, on_line) -> int:  # noqa: ANN001
        """A setup step outside the Sandbox (core/sandboxed.py asks for umu's runtime first)."""
        return stream(argv, env, on_line=on_line, cancel=self.ctx.cancel)

    def _run_in_prefix(self, argv: list[str]) -> int:
        """Run ``argv`` inside this profile's Wine/Proton prefix."""
        cmd, env = self._prefix_command(list(argv))
        return stream(cmd, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel)

    def _run_vcredist(self) -> None:
        """Best-effort VC++ runtime step on an existing prefix.

        This backend never creates a prefix, so the step runs only when the
        profile sets ``"vcredist": true``.
        """
        if not as_bool(profile_setting(self.ctx.profile, "vcredist"), False):
            return
        vcredist.ensure(
            self._prefix(),
            profile=self.ctx.profile,
            cache_dir=paths.cache_dir() / "wine",
            run_in_prefix=self._run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        configured = (
            profile_setting(self.ctx.profile, "exe_name")
            or profile_setting(self.ctx.profile, "launch_exe")
            or profile_setting(self.ctx.profile, "script")
        )
        target: Path | None
        try:
            target = self._target()
        except BackendError:
            target = None
        pids = scan_pids(self._patterns(target))
        runner = self._runner()
        data = {
            "exe": str(target) if target else "",
            "prefix": str(self._prefix()),
            "runner": runner or "",
            "pids": pids,
            "renderer": getattr(self, "_renderer", None) or "",
            "can_update": False,
        }
        if target is None:
            data["can_install"] = False  # wine never installs; set the exe instead
            if not configured:
                return Status(
                    state="error",
                    message=(
                        "No game executable is set for this profile. Open Settings → "
                        "Game options and choose the .exe."
                    ),
                    data=data,
                )
            return Status(state="not_installed", message="game executable not found", data=data)
        runtime_error = self._runtime_error()
        if runtime_error is not None and not pids:
            data["can_install"] = False
            return Status(state="error", message=runtime_error, data=data)
        note = graphics.vulkan_note()
        if pids:
            message = "game is running"
        else:
            message = "installed"
        if note:
            message = f"{message} — {note}"
        return Status(
            state="running" if pids else "installed",
            running=bool(pids),
            message=message,
            data=data,
        )

    def install(self) -> None:
        raise BackendError(
            "This profile expects an already-installed game. Add the game's .exe in "
            "Settings → Game options, or remove this profile."
        )

    def update(self) -> None:
        raise BackendError("the wine backend does not update games")

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        self._run_vcredist()
        try:
            argv, env, _prefix, target = self.build()
            runner = self._runner()
            tag = "Proton" if runner else "Wine"
            self.ctx.emit(Event("step", f"{tag}: launching {target.name}"))
            self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = graphics.RendererProbe(
                lambda: scan_pids(self._patterns()),
                on_result=lambda label: self.info(f"renderer: {label}"),
            ).start()
            try:
                return stream(
                    argv, env, on_line=lambda line: self.info(mask(line)), cancel=self.ctx.cancel
                )
            finally:
                probe.stop()
                probe.join(timeout=1.0)
                self._renderer = probe.result
        finally:
            self._stop_link_relay()

    def stop(self) -> None:
        pids = scan_pids(self._patterns())
        if pids:
            self.info(f"stopping {len(pids)} process(es): {pids}")
            stop_pids(pids)
        proton: str | None = None
        runner = self._runner()
        if runner:
            if runner in runners.RUNNERS:
                pinned = runners.installed_pinned(runner)
                proton = str(pinned) if pinned is not None else None
            else:
                try:
                    proton = str(self._runner_path(runner))
                except BackendError:
                    proton = None
        stop_runner_wineserver(
            prefix=self._prefix(), proton=proton, wine_binary=self._wine_binary()
        )

    def verify(self) -> dict:
        try:
            target = self._target()
        except BackendError as exc:
            return {"verified": False, "message": str(exc)}
        return {"verified": True, "exe": str(target), "prefix": str(self._prefix())}
