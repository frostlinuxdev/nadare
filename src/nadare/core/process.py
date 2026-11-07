# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Subprocess helpers, child environments, running-process detection and locks.

Backends are non-interactive: they block and report through an ``Emitter``. This
module gives them one place to spawn children, forward their output as ``Event``
lines, react to cancellation, build a clean Wine/Proton environment, find a
running game by name/prefix/path, and take a per-profile lock so two mutating
operations never run at once.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .events import Cancel, Emitter, Event, emitter, mask

#: How long ``run`` waits after a cancel before escalating to SIGKILL.
GRACE_SECONDS = 5.0
#: How long ``run`` waits for a SIGKILLed child to be reaped before giving up.
REAP_SECONDS = 5.0
LOCK_DIR_NAME = "locks"
log = logging.getLogger(__name__)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# child environment
# ---------------------------------------------------------------------------
_BUNDLE_QT_VARS = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


def restore_host_env(environ: dict[str, str] | Any = os.environ) -> None:
    """Undo the PyInstaller bootloader's ``LD_LIBRARY_PATH`` in a frozen build.

    The bootloader points ``LD_LIBRARY_PATH`` at the bundle's private libs and
    keeps the caller's value in ``LD_LIBRARY_PATH_ORIG``. Left alone, every child
    (Proton, Wine, host tools) would load those libs too, e.g. Proton's Python
    loses SSL. This process's loader already has its paths, so restoring the
    host value only affects children. A frozen ``sys.executable -m`` helper gets
    the bundle path again from its own bootloader.
    """
    orig = environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        environ["LD_LIBRARY_PATH"] = orig
    else:
        environ.pop("LD_LIBRARY_PATH", None)
    # The PyQt6 runtime hook points Qt at the bundle; host children (xdg-open
    # and other Qt apps) must not load those plugins.
    for key in _BUNDLE_QT_VARS:
        environ.pop(key, None)


def _xdg_home(value: Path) -> str:
    """``paths`` return ``<XDG>/nadare``; the env var is the parent."""
    return str(value.parent)


#: Python-launcher variables a game/Proton child must not inherit: Proton's own
#: ``proton`` script is ``#!/usr/bin/env python3`` and is sensitive to them.
_PYTHON_ENV_VARS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "VIRTUAL_ENV",
    "PIP_REQUIRE_VIRTUALENV",
)
#: Version-manager shim directories (they resolve ``python3`` through a launcher
#: that may not exist inside a container or after an uninstall).
_SHIM_MARKERS = (
    "/.pyenv/shims",
    "/.asdf/shims",
    "/.rbenv/shims",
    "/mise/shims",
    "/.local/share/shims",
)
_SYSTEM_DIRS = ("/usr/bin", "/bin")
#: Credential-looking variables (matched case-insensitively) never reach a game.
_SECRET_SUFFIXES = (
    "_TOKEN",
    "_SECRET",
    "_API_KEY",
    "_APIKEY",
    "_ACCESS_KEY",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIALS",
)
_SECRET_PREFIXES = ("AWS_", "SSH_", "GPG_", "CZN_")


def _system_python_exists() -> bool:
    return any(os.access(os.path.join(d, "python3"), os.X_OK) for d in _SYSTEM_DIRS)


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(_SECRET_SUFFIXES) or upper.startswith(_SECRET_PREFIXES)


def sanitize_game_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` that is safe to hand to Proton/Wine/umu.

    Always drops credential-looking variables (``*_TOKEN``, ``*_SECRET``, ``*_API_KEY``,
    ``*_PASSWORD`` and the like, and ``AWS_``/``SSH_``/``GPG_``/``CZN_`` names; a bare ``*KEY``
    stays, it names harmless graphics and input variables) and relative ``PATH`` entries (``.``
    lets a cwd binary shadow a bare Proton/umu helper). When there is a system
    ``python3`` it also drops the Python launcher variables and takes
    version-manager shims and an active virtualenv's ``bin`` off ``PATH`` (a stale
    ``pyenv`` shim ahead of ``/usr/bin`` makes ``proton``'s ``env python3`` exit
    127). That last cleanup is skipped when there is no system ``python3`` to fall
    back to, so a machine whose only Python is a shim keeps working.
    """
    out = {key: value for key, value in env.items() if not _is_secret_name(key)}
    venv = out.get("VIRTUAL_ENV", "").rstrip("/")
    has_system_python = _system_python_exists()
    if has_system_python:
        for name in _PYTHON_ENV_VARS:
            out.pop(name, None)
    kept: list[str] = []
    for entry in out.get("PATH", "").split(":"):
        norm = entry.rstrip("/")
        if not norm.startswith("/"):
            continue
        if (
            has_system_python
            and any(marker in norm + "/" or norm.endswith(marker) for marker in _SHIM_MARKERS)
        ):
            continue
        if has_system_python and venv and (norm == venv + "/bin" or norm.startswith(venv + "/")):
            continue
        kept.append(entry)
    if has_system_python:
        for system in _SYSTEM_DIRS:
            if system not in kept:
                kept.append(system)
    if "PATH" in out:
        out["PATH"] = ":".join(kept)
    return out


#: Token names that belong to the launcher (or another host tool), never to a game.
_SECRET_ENV_NAMES = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "SGDB_API_KEY"})
_SECRET_ENV_SUFFIXES = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def child_environ(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) without launcher secrets.

    Games, installers and Wine/Proton children must not see the user's GitHub
    token or any other ``NADARE_*`` credential. Deliberate paths such as
    ``CZN_CREDENTIAL_IN``/``CZN_CREDENTIAL_OUT`` and harmless ``NADARE_*``
    switches are kept.
    """
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in _SECRET_ENV_NAMES or (
            name.startswith("NADARE_") and name.endswith(_SECRET_ENV_SUFFIXES)
        ):
            del env[name]
    return env


#: Launch variables a backend pins itself. A profile or caller ``extra_env`` must
#: not move them, or the game starts in a different prefix than the one status,
#: Stop and the vcredist install use.
LAUNCH_RESERVED_ENV = frozenset({"WINEPREFIX", "STEAM_COMPAT_DATA_PATH", "PROTONPATH"})


def apply_extra_env(
    env: dict[str, str],
    extra: Mapping[str, str] | None,
    *,
    reserved: Iterable[str] = (),
) -> dict[str, str]:
    """Merge ``extra`` into ``env``; a ``reserved`` variable keeps its value.

    A conflicting reserved value is ignored with a warning instead of silently
    redirecting the launch.
    """
    for key, value in (extra or {}).items():
        key, value = str(key), str(value)
        if key in reserved:
            if env.get(key) != value:
                log.warning("ignoring %s from extra env: the launch pins it", key)
            continue
        env[key] = value
    return env


def build_env(
    profile: Mapping[str, Any] | None = None,
    extra: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment.

    The caller's environment is inherited, minus launcher secrets (see
    :func:`child_environ`), so ``DISPLAY``, ``WAYLAND_DISPLAY``, ``XAUTHORITY``,
    ``DBUS_SESSION_BUS_ADDRESS`` and friends survive; Nadare's XDG roots are then
    pinned so children never write to a hard-coded ``~/.local``. Profile
    ``prefix``/``runner`` become ``WINEPREFIX``/``PROTONPATH`` when set. ``extra``
    is merged last and can override anything except the profile's own prefix and
    runner: a declared
    prefix also pins ``WINEPREFIX`` and ``STEAM_COMPAT_DATA_PATH``, a declared
    runner pins ``PROTONPATH``. A conflicting ``extra`` value for those is ignored
    with a warning.
    """
    env: dict[str, str] = child_environ(base)

    env["XDG_DATA_HOME"] = _xdg_home(paths.data_dir())
    env["XDG_CONFIG_HOME"] = _xdg_home(paths.config_dir())
    env["XDG_STATE_HOME"] = _xdg_home(paths.state_dir())
    env["XDG_CACHE_HOME"] = _xdg_home(paths.cache_dir())
    if "XDG_RUNTIME_DIR" not in env and hasattr(os, "getuid"):
        runtime = Path(f"/run/user/{os.getuid()}")
        try:
            if runtime.is_dir() and runtime.stat().st_uid == os.getuid():
                env["XDG_RUNTIME_DIR"] = str(runtime)
        except OSError:
            pass

    reserved: set[str] = set()
    if profile:
        prefix = str(profile.get("prefix") or "").strip()
        if prefix:
            env["WINEPREFIX"] = str(Path(prefix).expanduser())
            reserved.update(("WINEPREFIX", "STEAM_COMPAT_DATA_PATH"))
        runner = str(profile.get("runner") or "").strip()
        if runner:
            env["PROTONPATH"] = str(Path(runner).expanduser())
            reserved.add("PROTONPATH")
        env.setdefault("WINEDEBUG", env.get("WINEDEBUG", "-all"))

    apply_extra_env(env, extra, reserved=reserved)
    return env


# ---------------------------------------------------------------------------
# process detection
# ---------------------------------------------------------------------------
@dataclass
class ProcessInfo:
    pid: int
    name: str
    cmdline: list[str] = field(default_factory=list)
    exe: str | None = None

    @property
    def joined(self) -> str:
        return " ".join(self.cmdline)


def _read_cmdline(
    pid: int, root: Path = Path("/proc")
) -> tuple[list[str], str | None, str]:
    base = root / str(pid)
    try:
        raw = (base / "cmdline").read_bytes()
    except OSError:
        return [], None, ""
    parts = [p for p in raw.split(b"\x00") if p]
    cmdline = [p.decode("utf-8", "replace") for p in parts]
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = None
    try:
        # The kernel cuts comm at 15 bytes, which can split a multi-byte character (#928).
        name = (base / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        # Fall back to the first token of the command line.
        name = Path(cmdline[0]).name if cmdline else ""
    return cmdline, exe, name


def iter_processes() -> Iterator[ProcessInfo]:
    """Yield the current process table from ``/proc`` (Linux only)."""
    try:
        entries = sorted(p.name for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return
    for entry in entries:
        try:
            pid = int(entry)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        cmdline, exe, name = _read_cmdline(pid)
        if not cmdline and not exe:
            continue
        yield ProcessInfo(pid=pid, name=name or Path(cmdline[0]).name, cmdline=cmdline, exe=exe)


@dataclass(frozen=True)
class ProcessRecord:
    """One same-user process captured for a shared status-probe round."""

    pid: int
    argv: list[str]
    environ: dict[str, str]
    started: str | None


class ProcessScanRound:
    """Lazy, immutable process snapshot shared only within an explicit probe round."""

    def __init__(self, proc_root: str | os.PathLike[str] = "/proc") -> None:
        self.proc_root = Path(proc_root)
        self._lock = threading.Lock()
        self._records: tuple[ProcessRecord, ...] | None = None

    def records(self) -> tuple[ProcessRecord, ...]:
        if self._records is not None:
            return self._records
        with self._lock:
            if self._records is not None:
                return self._records
            try:
                entries = list(self.proc_root.iterdir())
            except OSError:
                self._records = ()
                return self._records
            records: list[ProcessRecord] = []
            me, uid = os.getpid(), os.getuid()
            real_proc = self.proc_root == Path("/proc")
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == me:
                    continue
                try:
                    if entry.stat().st_uid != uid:
                        continue
                    started_before = _pid_starttime(pid) if real_proc else None
                    raw = (entry / "cmdline").read_bytes()
                    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
                    if not argv:
                        continue
                    try:
                        env_raw = (entry / "environ").read_bytes()
                    except OSError:
                        env_raw = b""
                    environ: dict[str, str] = {}
                    for part in env_raw.split(b"\0"):
                        if not part:
                            continue
                        key, _, value = part.partition(b"=")
                        environ[key.decode("utf-8", "replace")] = value.decode(
                            "utf-8", "replace"
                        )
                    started_after = _pid_starttime(pid) if real_proc else None
                    if started_after != started_before:
                        continue
                    records.append(ProcessRecord(pid, argv, environ, started_after))
                except OSError:
                    continue
            self._records = tuple(records)
            return self._records


_PROCESS_SCAN_LOCAL = threading.local()


@contextmanager
def use_process_scan_round(round_: ProcessScanRound):
    """Bind one snapshot to the current status worker thread."""
    previous = getattr(_PROCESS_SCAN_LOCAL, "round", None)
    _PROCESS_SCAN_LOCAL.round = round_
    try:
        yield
    finally:
        _PROCESS_SCAN_LOCAL.round = previous


def current_process_scan_round() -> ProcessScanRound | None:
    """Return the snapshot bound to this worker, if it belongs to a probe round."""
    value = getattr(_PROCESS_SCAN_LOCAL, "round", None)
    return value if isinstance(value, ProcessScanRound) else None


def _under_prefix(value: str, prefix: str) -> bool:
    """True when ``value`` is ``prefix`` or a path *below* it.

    A bare ``startswith`` lets ``/a/b`` match ``/a/bc``; require exact equality
    or a path separator at the boundary.
    """
    if value == prefix:
        return True
    if not value.startswith(prefix):
        return False
    return value[len(prefix) :].startswith(os.sep)


def find_processes(
    *,
    prefix: str | os.PathLike[str] | None = None,
    name: str | None = None,
    cmdline_contains: str | None = None,
) -> list[ProcessInfo]:
    """Return processes matching any supplied criterion.

    ``prefix`` matches the executable path or any argument that is the prefix or
    sits below it (e.g. a game install directory); ``name`` matches the basename
    of the executable or the kernel ``comm``; ``cmdline_contains`` is a
    case-insensitive substring of the full command line.
    """
    prefix_s = str(Path(prefix).expanduser()) if prefix else None
    needle = cmdline_contains.lower() if cmdline_contains else None
    out: list[ProcessInfo] = []
    for proc in iter_processes():
        if name:
            first = Path(proc.cmdline[0]).name if proc.cmdline else ""
            if proc.name != name and first != name:
                continue
        if prefix_s:
            candidates = [proc.exe or ""] + proc.cmdline
            if not any(_under_prefix(c, prefix_s) for c in candidates):
                continue
        if needle and needle not in proc.joined.lower():
            continue
        out.append(proc)
    return out


def is_running(
    *,
    prefix: str | os.PathLike[str] | None = None,
    name: str | None = None,
    cmdline_contains: str | None = None,
) -> bool:
    return bool(find_processes(prefix=prefix, name=name, cmdline_contains=cmdline_contains))


def _pid_starttime(pid: int, root: Path = Path("/proc")) -> str | None:
    """Start time (clock ticks since boot) of ``pid``, or ``None`` if unreadable.

    Two different processes never share a start time, so a changed value across
    the grace window means the PID was recycled onto an unrelated process.
    """
    try:
        data = (root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        # Field 22; the comm (field 2) may contain spaces/parens, so split on
        # the last ')' and index from there (field 3 is index 0).
        return data.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def terminate(pid: int, *, grace: float = GRACE_SECONDS) -> bool:
    """SIGTERM a pid, escalating to SIGKILL. Returns True if it had to be killed."""
    started = _pid_starttime(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.1)
    # Revalidate identity before escalating: if the process exited and the PID
    # was recycled, SIGKILL must not land on a bystander.
    if started is not None:
        now = _pid_starttime(pid)
        if now is None or now != started:
            return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------------------
# spawning / running
# ---------------------------------------------------------------------------
def spawn(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.STDOUT,
    start_new_session: bool = True,
    **popen_kwargs: Any,
) -> subprocess.Popen[str]:
    """Start a child process with sane defaults (own session, no stdin).

    ``start_new_session=True`` keeps Ctrl-C on the terminal from racing the
    launcher's own handler and lets us signal the whole process group.
    """
    cmd = [str(a) for a in args]
    # A child may write bytes that are not valid text (a game's legacy-encoded string): decoding
    # them strictly kills the reader thread, loses the rest of the output and, once the pipe is
    # full, blocks the child (#976).
    popen_kwargs.setdefault("errors", "replace")
    return subprocess.Popen(  # noqa: S603 - args are backend-controlled
        cmd,
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
        start_new_session=start_new_session,
        **popen_kwargs,
    )


def run(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    capture: bool = False,
    check: bool = False,
    stream: bool = True,
    forward_stderr: bool = True,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a child to completion, forwarding output and honouring cancellation.

    ``emit`` receives one ``log`` event per output line (unless ``stream`` is
    False), a ``status`` event for the spawn, and an ``error`` event on failure.
    ``capture`` collects the same lines on ``CompletedProcess.stdout``. On
    cancel the child's process group is told to stop and ``Cancelled`` is raised.
    """
    say = emitter(emit)
    say(Event("status", mask(f"$ {' '.join(str(a) for a in args)}"), data={"stage": "spawn"}))

    stderr_mode = subprocess.STDOUT if forward_stderr else subprocess.PIPE
    proc = spawn(args, env=env, cwd=cwd, stderr=stderr_mode, **popen_kwargs)
    # Keep the group id: once the direct child is reaped ``getpgid(proc.pid)``
    # fails, but the group can still hold grandchildren we must signal.
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    lines: list[str] = []
    # Bounded so a chatty child cannot pile its whole output up in memory when
    # the caller did not ask for capture; the reader blocks until we drain.
    line_q: queue.Queue[str | None] = queue.Queue(maxsize=1024)

    def _reader() -> None:
        if proc.stdout is None:
            raise RuntimeError("child process has no stdout pipe")
        try:
            for line in proc.stdout:
                line_q.put(line.rstrip("\n"))
        finally:
            line_q.put(None)

    reader = threading.Thread(target=_reader, name="nadare-run-reader", daemon=True)
    reader.start()

    def _drain() -> None:
        while True:
            try:
                item = line_q.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            if capture:
                lines.append(item)
            if stream:
                say(Event("log", item))

    def _wait_reaped() -> None:
        try:
            proc.wait(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning("child %s not reaped %ss after SIGKILL", proc.pid, REAP_SECONDS)

    try:
        cancelled = False
        while proc.poll() is None:
            if cancel is not None and cancel.cancelled:
                cancelled = True
                _signal_group(proc)
                break
            try:
                item = line_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is not None:
                if capture:
                    lines.append(item)
                if stream:
                    say(Event("log", item))

        try:
            proc.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(proc, force=True, pgid=pgid)
            _wait_reaped()
        if cancelled:
            # The direct child may have exited on SIGTERM while a grandchild ignored
            # it and keeps the stdout pipe open. Escalate after the wait so the
            # group is gone and the reader thread can end with this operation.
            _signal_group(proc, force=True, pgid=pgid)
        # Drain while the reader winds down: with a bounded queue it may be blocked
        # on a put until we take items out.
        deadline = time.monotonic() + (5.0 if cancelled else 1.0)
        while reader.is_alive() and time.monotonic() < deadline:
            _drain()
            reader.join(timeout=0.05)
        _drain()
        if reader.is_alive():
            # The direct child is gone but a grandchild (a game or wineserver that
            # outlived its launcher) still holds the pipe. Keep consuming it in the
            # background: leaving the bounded queue full would block the child on
            # its next write, and closing our end would turn that write into
            # SIGPIPE. The reader ends when the last writer closes the pipe.
            def _drain_rest() -> None:
                while reader.is_alive():
                    try:
                        line_q.get(timeout=0.2)
                    except queue.Empty:
                        continue

            threading.Thread(
                target=_drain_rest, name="nadare-run-drain", daemon=True
            ).start()

        cp = subprocess.CompletedProcess(
            list(str(a) for a in args),
            proc.returncode,
            "\n".join(lines) if capture else None,
            None,
        )
        if cancelled:
            raise _cancelled_exc()
        if check and cp.returncode != 0:
            say(
                Event(
                    "error",
                    f"command failed ({cp.returncode}): {cp.args[0]}",
                    data={"code": cp.returncode},
                )
            )
            raise subprocess.CalledProcessError(cp.returncode, cp.args, cp.stdout)
        return cp
    except BaseException:
        # An interrupt (a second Ctrl-C) or a raising emitter must not unwind
        # past a live child: it would outlive the CLI and release the profile
        # lock while still writing to the same files.
        if proc.poll() is None:
            _signal_group(proc, pgid=pgid)
            try:
                try:
                    proc.wait(timeout=GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                # Always escalate the group, even if this wait is itself interrupted:
                # the leader may have exited on SIGTERM while a grandchild ignored it
                # and still holds the profile's files.
                _signal_group(proc, force=True, pgid=pgid)
                _wait_reaped()
        else:
            # The leader already exited, but its descendants (a game, wineserver) may still be
            # in its group and would keep running while the caller releases the profile (#896).
            _stop_orphaned_group(pgid)
        raise


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def _stop_orphaned_group(pgid: int | None) -> None:
    """Terminate, then kill, whatever is left in ``pgid`` after its leader exited.

    The group id is the one this call started (its leader's pid); while any member lives the
    kernel cannot hand that number to another group, so this only ever reaches our own
    descendants. Nothing is signalled when the group is already empty."""
    if pgid is None or not _group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + min(GRACE_SECONDS, 2.0)
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(0.05)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def _signal_group(
    proc: subprocess.Popen[Any], *, force: bool = False, pgid: int | None = None
) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass


def _cancelled_exc() -> BaseException:
    from .events import Cancelled

    return Cancelled("operation cancelled")


def which(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    return shutil.which(name, path=(env or os.environ).get("PATH"))


# ---------------------------------------------------------------------------
# per-profile lock
# ---------------------------------------------------------------------------
class ProfileLock:
    """An advisory ``flock`` guarding mutating operations on one profile.

    Usage::

        with ProfileLock("my-game"):
            ...

    ``acquire`` is non-blocking by default; pass ``wait`` seconds to poll. A
    lost lock raises :class:`ProfileLockError`.
    """

    #: File suffix; subclasses use another one so their lock is independent.
    suffix = ".lock"

    def __init__(self, profile: str, *, wait: float = 0.0, poll: float = 0.2) -> None:
        self.profile = profile
        self.wait = wait
        self.poll = poll
        self.path = paths.state_dir() / LOCK_DIR_NAME / f"{_safe(profile)}{self.suffix}"
        self._fd: int | None = None

    def acquire(self) -> ProfileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(self.wait, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise ProfileLockError(f"profile {self.profile!r} is busy") from None
                time.sleep(self.poll)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ProfileLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class OperationLock(ProfileLock):
    """Serialises install, update, repair, remake-prefix and Remove per profile.

    A separate lock file from :class:`ProfileLock`, which CZN holds for token
    rotation inside those same operations, so the two never block each other.
    """

    suffix = ".ops.lock"


#: What the user sees when an :class:`OperationLock` is already held.
OPERATION_BUSY = (
    "Another Nadare window or command is installing, updating or removing {name}. "
    "Wait for it to finish and try again."
)

#: What the user sees when a launch is refused because an operation is running.
OPERATION_IN_PROGRESS = (
    "An install/update/repair for {name} is in progress; wait for it to finish, then launch again."
)


def operation_in_progress(profile: str) -> bool:
    """Whether an :class:`OperationLock` for ``profile`` is currently held.

    A launch may run for hours, so it must not take the lock itself; it only
    probes it without blocking. The probe opens the existing lock file, tries
    the lock and releases or closes it immediately, so it never keeps the lock.
    """
    lock = OperationLock(profile)
    try:
        fd = os.open(lock.path, os.O_RDWR)
    except OSError:
        return False  # no lock file means nobody ever took the lock
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Only "somebody holds it" counts. A filesystem that cannot lock at all
            # (ENOLCK, ENOTSUP...) must not make every launch look refused forever.
            return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class ProfileLockError(RuntimeError):
    """Another Nadare operation already holds the profile lock."""


def _safe(name: str) -> str:
    """Map a profile name to a lock filename: readable slug + name digest.

    A plain character collapse is many-to-one (``a b`` and ``a_b`` would share a
    lock file), so a digest of the original name is appended to keep distinct
    profiles on distinct locks.
    """
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in name]
    slug = "".join(keep).strip("._") or "profile"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:100]}-{digest}"
