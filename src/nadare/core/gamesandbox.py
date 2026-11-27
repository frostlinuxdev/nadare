# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Sandbox: run a game with the real home folder hidden (bubblewrap).

A Windows process under Wine can read the whole Linux home through ``Z:`` or the
``\\\\?\\unix\\`` path, and drive letters cannot be taken away (measured, #804). What does
work is a mount namespace: the home folder is replaced by an empty tmpfs and only the folders
the game needs are bound back. This module builds that command; it never runs anything.

Not in ``core/sandbox.py``: that one is about running inside Flatpak.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import diewithparent, hostlibs, linkrelay, paths, prefixguard, process, sandbox, userns
from .config import as_bool
from .paths import inside as _inside

log = logging.getLogger(__name__)

BINARY = "bwrap"

#: An unset ``secure_mode`` means on for games that support the Sandbox.
DEFAULT_ON = True

#: Other drives and removable media: hidden so a game cannot read them.
_HIDDEN_MOUNTS = ("/mnt", "/media", "/run/media")

#: What of the session runtime folder a game still gets: the display (wayland-*, gamescope-*)
#: and the sound.
#: The session bus, the keyring and the ssh/gpg agents are deliberately not here.
_RUNTIME_KEEP = ("pulse", "pipewire-0", "pipewire-0-manager")

#: Variables that point at sockets Sandbox hides; left set, the game's own container
#: (pressure-vessel) would try to bind them and stop.
_HIDDEN_ENV = ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO")


def _is_loader_env(name: str) -> bool:
    """A dynamic loader variable, which must not apply to bwrap or the host shell."""
    return name.startswith("LD_")


#: The same rule, for code that runs a host helper into the Sandbox (sandboxjoin, #1766).
is_loader_env = _is_loader_env


#: Where a Sandboxed game's shader caches go: its own folder inside Nadare's cache folder, so
#: they survive between launches without showing the game the rest of ``~/.cache``.
_CACHE_FOLDER = "sandbox-cache"


class BroadBindError(ValueError):
    """A game folder is so broad that binding it would give the hidden home folder back."""


def _as_bool(value: object) -> bool | None:
    """A real boolean, or a hand-edited string that clearly means one (``"false"``)."""
    return as_bool(value)


def choice(profile: Mapping[str, object]) -> bool | None:
    """The explicit Sandbox answer in ``profile``, or ``None`` when it is unset.

    The config loader turns a hand-edited top-level ``"false"`` into a boolean but leaves the
    ``extra`` mapping alone, so a string there is read here the same way.
    """
    value = _as_bool(profile.get("secure_mode"))
    if value is not None:
        return value
    extra = profile.get("extra")
    if isinstance(extra, Mapping):
        return _as_bool(extra.get("secure_mode"))
    return None


def wanted(
    profile: Mapping[str, object],
    *,
    supported: bool,
    default: bool | None = None,
    required: bool = False,
) -> bool:
    """Whether the Sandbox should run: supported, then required, then the explicit choice,
    then the default.

    ``default`` is the backend kind's own default. ``False`` keeps a kind off whatever
    ``DEFAULT_ON`` says; ``None`` or ``True`` follows ``DEFAULT_ON`` (so the test suite can
    turn every such kind off at once). A ``required`` kind (Duet Night Abyss, #1330) is always
    on: no saved choice turns it off.
    """
    if not supported:
        return False
    if required:
        return True
    answer = choice(profile)
    if answer is not None:
        return answer
    if default is False:
        return False
    return DEFAULT_ON


def runtime_copies() -> list[str]:
    """The ``srt-bwrap`` copies in the Steam Runtime that umu downloads for Proton, newest first.

    Every Proton game Nadare runs already has one on disk, so Sandbox needs nothing extra.
    """
    return userns.permitted_runtime_copies(
        [path for path in userns._srt_bwrap_paths() if os.access(path, os.X_OK)]
    )


def available() -> bool:
    """True when some ``bwrap`` exists (the runtime's copy or the system's). Never raises."""
    return bool(runtime_copies()) or shutil.which(BINARY) is not None


#: The answer of :func:`ready` for this run of Nadare (``{"ready": bool}``).
_READY_CACHE: dict[str, object] = {}


def ready_cached() -> bool | None:
    """:func:`ready`'s answer when it is already known (no probe), else ``None``.

    For the game page: the probe starts a program, which must not run on the GUI thread (#1207
    b191); the page asks :func:`ready` in the background when this says ``None``.
    """
    if sandbox.in_flatpak():
        return None if "flatpak" not in _READY_CACHE else bool(_READY_CACHE["flatpak"])
    try:
        key: object = pins_path().stat().st_mtime_ns
    except OSError:
        key = None
    if _READY_CACHE.get("key", object()) != key or "ready" not in _READY_CACHE:
        return None
    return bool(_READY_CACHE["ready"])


def ready() -> bool:
    """Whether a bubblewrap that really starts exists here.

    :func:`available` only says a file exists; on Ubuntu 24.04 a blocked bubblewrap exists but
    cannot start, and the page must not claim a Sandbox the launch cannot give. This is a page
    question, so it never records a runtime copy (only the Sandbox's first real start may, see
    :func:`trusted_runtime_copies`) and never runs an unrecorded one. The answer is kept until the
    recorded copies change, so the first sandboxed launch updates it without a restart.
    """
    if sandbox.in_flatpak():
        # Flatpak itself provides the sandbox; no bubblewrap probe is needed.
        answer = _flatpak_spawn() is not None
        _READY_CACHE["flatpak"] = answer
        return answer
    try:
        key: object = pins_path().stat().st_mtime_ns
    except OSError:
        key = None
    if _READY_CACHE.get("key", object()) != key or "ready" not in _READY_CACHE:
        try:
            answer = locate(record=False).binary is not None
        except Exception:  # noqa: BLE001 - unknown means not ready
            answer = False
        _READY_CACHE.update(key=key, ready=answer)
    return bool(_READY_CACHE["ready"])


#: Where Nadare records which ``srt-bwrap`` copies it trusts (path and sha256). Outside every
#: folder a Sandboxed game can write to.
PINS_FILE = "sandbox-runtime.json"


def pins_path() -> Path:
    return paths.state_dir() / PINS_FILE


def _sha256(path: str) -> str | None:
    from . import download

    try:
        return download.hash_file(path)["sha256"]
    except OSError:
        return None


def _read_pins() -> dict[str, str] | None:
    """The recorded copies, ``None`` when nothing was recorded yet, ``{}`` when it is damaged.

    An empty record (``{}``) is "nothing recorded yet": an older Nadare wrote one when the
    Sandbox first started before umu had downloaded its runtime, and then never trusted the copy
    that arrived (Ubuntu VM, 2026-11-14). A damaged file still trusts nothing.
    """
    try:
        text = pins_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    if not value:
        return None
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _record_runtime_copies() -> dict[str, str]:
    pins = {copy: digest for copy in runtime_copies() if (digest := _sha256(copy))}
    if not pins:
        # Nothing on disk yet (umu has not downloaded its runtime): record nothing, so the copy
        # that arrives is recorded at the next start instead of being refused forever.
        return pins
    target = pins_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(pins, indent=1), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        pass  # nothing recorded: the next start asks again, and no copy is run without a record
    return pins


def trusted_runtime_copies() -> list[str]:
    """The runtime ``srt-bwrap`` copies Nadare recorded and that still match, newest first.

    Nadare runs this file outside the Sandbox, and the folder it sits in is writable by the
    game, so a game could plant a newer copy for the next launch (#871). Copies present the
    first time the Sandbox is used are recorded then (nothing Sandboxed has run before that);
    a copy that appears later, or whose content changed, is never run.
    """
    pins = _read_pins()
    if pins is None:
        pins = _record_runtime_copies()
    return [copy for copy in runtime_copies() if pins.get(copy) and _sha256(copy) == pins[copy]]


def recorded_runtime_copies() -> list[str]:
    """Like :func:`trusted_runtime_copies`, but never records anything.

    For callers that only want to look (``nadare doctor``): the first call to
    ``trusted_runtime_copies`` pins whatever is on disk then, and that must be the Sandbox's
    first start, not a diagnostic that a game could have been waiting for (#965).
    """
    pins = _read_pins()
    if not pins:
        return []
    return [copy for copy in runtime_copies() if pins.get(copy) and _sha256(copy) == pins[copy]]


def untrusted_runtime_copies() -> list[str]:
    """Runtime copies on disk that the record does not cover (changed, or new), newest first.

    Empty while nothing was recorded yet: the Sandbox's first start records what is there. A
    non-empty answer is usually umu's runtime update (every few weeks), but a game can write
    to that folder too, so only the player decides to trust it (:func:`trust_runtime_copies`).
    Never records anything.
    """
    pins = _read_pins()
    if not pins:
        return []
    return [copy for copy in runtime_copies() if not pins.get(copy) or _sha256(copy) != pins[copy]]


class RuntimeBusy(RuntimeError):
    """A program from the game runtime is running, so its files are not trusted right now."""


def runtime_in_use(
    copies: Iterable[str] | None = None, *, proc_root: str | os.PathLike[str] = "/proc"
) -> bool:
    """Whether a process of this user runs from a runtime folder (a game, umu, pressure-vessel).

    A game running in the Sandbox can write to the runtime folder, so a copy is never trusted
    while one runs.
    """
    roots = []
    for copy in runtime_copies() if copies is None else copies:
        parents = Path(copy).parents
        if len(parents) > 3:
            roots.append(str(parents[3]) + os.sep)  # umu/<runtime>/
    if not roots:
        return False
    for record in process.ProcessScanRound(proc_root).records():
        if any(arg.startswith(root) for arg in record.argv for root in roots):
            return True
    return False


def trust_runtime_copies(copies: Iterable[str]) -> list[str]:
    """Record ``copies`` (runtime copies on disk only) as trusted; the ones recorded.

    The player's answer to "the game runtime was updated": other records are kept. Refused with
    :class:`RuntimeBusy` while a runtime program runs, because a running game could be writing
    the file being recorded.
    """
    on_disk = runtime_copies()
    wanted = [copy for copy in dict.fromkeys(copies) if copy in on_disk]
    if not wanted:
        return []
    if runtime_in_use(on_disk):
        raise RuntimeBusy("a game or the game runtime is running")
    pins = dict(_read_pins() or {})
    recorded = []
    for copy in wanted:
        digest = _sha256(copy)
        if digest:
            pins[copy] = digest
            recorded.append(copy)
    if not recorded:
        return []
    target = pins_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(pins, indent=1), encoding="utf-8")
    os.replace(temp, target)
    _READY_CACHE.clear()
    return recorded


def trust_question() -> list[str]:
    """The copies to offer for trust: only when the Sandbox has no working bubblewrap left.

    With a working system bubblewrap the Sandbox runs anyway, so nobody is asked. Probes
    programs: never on the GUI thread.
    """
    if sandbox.in_flatpak():
        return []
    located = locate(record=False)
    if located.binary is not None:
        return []
    return list(located.untrusted)


@dataclass(frozen=True)
class Located:
    """The bubblewrap Sandbox will use, or why there is none."""

    binary: str | None  # what to run; None when nothing works
    source: str = ""  # "runtime" or "system"
    found: bool = False  # some copy exists (so a failure is "blocked", not "not installed")
    why: str = ""  # the first reason a copy could not start
    #: Runtime copies the record does not cover (usually umu updated its runtime).
    untrusted: tuple[str, ...] = ()


def locate(*, record: bool = True) -> Located:
    """Pick a bubblewrap that really starts here: the runtime's own copy first, then the system's.

    The runtime copy is tried first because it is already on disk, is the one the AppArmor profile
    Nadare installs on Ubuntu allows, and is the same one Proton's container uses. Only copies
    Nadare recorded are run (see :func:`trusted_runtime_copies`); with ``record=False`` (a page
    asking) nothing is recorded, so only copies an earlier Sandbox start recorded count.
    """
    trusted = trusted_runtime_copies() if record else recorded_runtime_copies()
    candidates = [(path, "runtime") for path in trusted]
    system = shutil.which(BINARY)
    if system is not None:
        # Run the absolute path that was probed: a bare name would be resolved again through
        # the launch environment's PATH, which the game can control.
        candidates.append((system, "system"))
    first_why = ""
    unrecorded = [copy for copy in runtime_copies() if copy not in trusted]
    for binary, source in candidates:
        started, why = works(binary)
        if started:
            return Located(binary, source, True)
        first_why = first_why or why
    untrusted = tuple(untrusted_runtime_copies())
    if untrusted:
        first_why = (
            "the game runtime was updated and Nadare has not been told to trust its new "
            "bubblewrap yet (a game could also have put it there); restart Nadare and answer "
            f'"Trust the new copy", or delete {pins_path()} and start again'
        )
    elif not first_why and unrecorded:
        first_why = (
            "the game runtime has a bubblewrap Nadare did not record, so it is not run "
            "(a game could have put it there). If you trust this runtime, delete "
            f"{pins_path()} and start again"
        )
    return Located(None, "", bool(candidates) or bool(unrecorded), first_why, untrusted)


def install_hint(family: str | None) -> str:
    """The command that installs bubblewrap on this kind of distro (a plain hint when unknown)."""
    template = hostlibs._INSTALL.get(family or "")
    if template is None:
        return "install the 'bubblewrap' package with your package manager"
    return template.replace("sudo dpkg --add-architecture i386 && sudo apt update && ", "").format(
        pkgs="bubblewrap"
    )


def works(binary: str = BINARY) -> tuple[bool, str]:
    """Whether ``binary`` can really start here, and the first line of why not.

    Ubuntu 24.04 and newer block unprivileged user namespaces for programs without an AppArmor
    profile; the profile Nadare installs covers the runtime's own copy.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [binary, "--die-with-parent", "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ""
    lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
    return False, lines[0] if lines else f"bwrap exited with {done.returncode}"


def _existing(path: str | Path | None) -> Path | None:
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.exists() else None


def _broad_reason(folder: Path, home: Path, *, allow_drive_top: bool = False) -> str:
    """Why binding ``folder`` would give the hidden home folder back, or an empty string."""
    return prefixguard.place_reason(  # the shared rule (#1229)
        folder, home=home, allow_drive_top=allow_drive_top
    )


def _bind_target(
    path: str | Path | None,
    home: Path,
    *,
    directory: bool = False,
    regular: bool = False,
    allow_drive_top: bool = True,
) -> tuple[Path | None, str]:
    """The resolved path if it may be bound, and why not (``""`` when it may).

    One rule for every path taken from the environment. A missing optional path is skipped
    quietly; a path that is too broad, or the wrong kind, is returned with its reason so the
    caller can log it or refuse the launch.
    """
    if not path:
        return None, ""
    target = _existing(path)
    reason = _broad_reason(
        target if target is not None else Path(path),
        home,
        allow_drive_top=allow_drive_top,
    )
    if target is None:
        return None, reason
    if not reason and directory and not target.is_dir():
        reason = "not a folder"
    if not reason and regular and not target.is_file():
        reason = "not a regular file"
    return (None, reason) if reason else (target, "")


#: Proton verbs that take the program as the next argument.
_PROTON_VERBS = frozenset({"run", "waitforexitandrun", "runinprefix"})

#: Proton maps the first library path that is part of the game's path as drive S:.
GAME_DRIVE_ENV = "STEAM_COMPAT_LIBRARY_PATHS"


def _proton_program(argv: list[str]) -> Path | None:
    """The Windows program a umu or ``proton <verb>`` command starts, or None."""
    for i, arg in enumerate(argv):
        name = Path(arg).name
        if name == "umu-run" or arg == "--umu-run":
            # The hidden `nadare --umu-run` wrapper also starts umu (#1341 item 3).
            rest = argv[i + 1 : i + 2]
        elif name == "proton" and argv[i + 1 : i + 2] and argv[i + 1] in _PROTON_VERBS:
            rest = argv[i + 2 : i + 3]
        else:
            continue
        if rest and os.path.isabs(rest[0]):
            return Path(rest[0])
        return None
    return None


def game_drive(
    argv: list[str],
    env: Mapping[str, str],
    *,
    is_mount: Callable[[str], bool] = os.path.ismount,
) -> str | None:
    """The folder Proton maps as drive S: when the game runs outside the Sandbox (#1614).

    umu walks up from the game's folder (``STEAM_COMPAT_INSTALL_PATH``, else the program's
    folder) to the first mount point below ``/`` and Proton maps it as S:. Inside the Sandbox
    the bound game folder and the hidden home are mount points of their own, so S: became the
    game folder and NTE's launcher saw itself "in the root directory of the disk". Asked here,
    on the host, before the Sandbox starts. ``/`` when nothing below it is a mount: then S:
    is the same as Z: and every path stays what it is outside the Sandbox.
    """
    program = _proton_program(argv)
    if program is None:
        return None
    start = Path(os.path.abspath(env.get("STEAM_COMPAT_INSTALL_PATH") or program.parent))
    for parent in start.parents:
        if str(parent) != "/" and is_mount(str(parent)):
            return str(parent)
    return "/"


def wrap(
    argv: list[str],
    env: Mapping[str, str],
    *,
    prefix: Path,
    install_dir: Path | None = None,
    runner_dir: Path | None = None,
    home: Path | None = None,
    link_pipe: Path | None = None,
    read_only: Iterable[Path] = (),
    bwrap: str = BINARY,
    share_pid: bool = False,
    host_tmp: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Return ``([bwrap, ...flags, "--", *argv], env)`` with the real home hidden.

    The home is resolved first (``readlink -f``): on Fedora Atomic ``/home`` is a symlink and
    bubblewrap cannot mount over it, but it can over what it points to, which hides the home
    through both paths. Paths that do not exist are skipped rather than failing the launch.
    ``read_only`` files (an installer Nadare downloaded) are bound back read-only after the
    other mounts, so they stay visible inside the hidden home.

    By default the game gets its own ``/tmp`` and PID namespace, and the session runtime is
    hidden except for the display and sound sockets. ``share_pid`` and ``host_tmp`` are the
    per-game opt-outs (``sandbox_share_pid`` / ``sandbox_host_tmp`` in the profile): the
    first is for a game that needs to see host processes, the second for a game that needs
    something Nadare cannot know about in the host's ``/tmp``.
    """
    real_home = Path(home or Path.home()).resolve()
    flags: list[str] = ["--die-with-parent", "--dev-bind", "/", "/", "--tmpfs", str(real_home)]
    if not share_pid:
        # The game (and umu, pressure-vessel, Wine's .wine-UID server before it) lives in its
        # own PID namespace. The fresh procfs is not optional: the bind above exposes the
        # host's /proc, where the game's own PID (and /proc/self) would not match its view.
        flags += ["--unshare-pid", "--proc", "/proc"]
    if not host_tmp:
        # umu, pressure-vessel and the game itself start inside this namespace, so their
        # pressure-vessel-* trees and Wine's .wine-UID server directory are created in this
        # same private /tmp: the server and its game always meet. Host /tmp is not bound,
        # except for the X11 socket directory, which must stay connectable, and the X
        # authority file below (bound back where it lives when that is under /tmp).
        flags += ["--tmpfs", "/tmp"]
        if Path("/tmp/.X11-unix").is_dir():
            flags += ["--ro-bind", "/tmp/.X11-unix", "/tmp/.X11-unix"]
    hidden: list[str] = []
    for mount in _HIDDEN_MOUNTS:
        # bwrap cannot mount on a symlink (/mnt -> var/mnt on Bazzite): use the target.
        target = str(Path(mount).resolve())
        if Path(target).is_dir() and target not in hidden:
            hidden.append(target)
            flags += ["--tmpfs", target]

    runtime, runtime_why = _bind_target(env.get("XDG_RUNTIME_DIR"), real_home, directory=True)
    if runtime_why:
        log.warning(
            "Sandbox: not using XDG_RUNTIME_DIR %s: %s",
            env.get("XDG_RUNTIME_DIR"),
            runtime_why,
        )
    if runtime is not None:
        flags += ["--tmpfs", str(runtime)]
        # Wayland and gamescope sockets (gamescope's Vulkan layer needs its own: without it a game
        # run inside gamescope never gets a swapchain and renders nothing).
        keep = [
            p
            for pattern in ("wayland-*", "gamescope-*")
            for p in runtime.glob(pattern)
            if not p.name.endswith((".lock", "-lock"))
        ]
        keep += [runtime / name for name in _RUNTIME_KEEP if (runtime / name).exists()]
        for path in sorted(set(keep)):
            flags += ["--bind", str(path), str(path)]

    bound: list[Path] = []

    def bind(path: Path | None, *, read_only: bool = False) -> None:
        if path is None or any(_inside(path, done) for done in bound):
            return
        bound.append(path)
        flags.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])

    hidden_roots = [real_home, *(Path(h) for h in hidden)]
    if not host_tmp:
        hidden_roots.append(Path("/tmp"))
    if runtime is not None:
        hidden_roots.append(runtime)

    shown: list[Path] = []  # bound read-only outside `bind` (Nadare's own downloads)

    def alias(raw: str | Path | None, *, read_only: bool = False) -> None:
        """Bind a path reached through a symlink in a hidden area at that path too.

        The command and Wine use the path as written (``~/Games`` -> another disk); inside the
        Sandbox the symlink is gone with the hidden home, so Wine would make an empty prefix there.
        """
        if not raw:
            return
        written = Path(os.path.abspath(Path(raw).expanduser()))
        target = _existing(raw)
        if target is None or written == target:
            return
        if not any(_inside(written, root) for root in hidden_roots):
            return
        # Only show again what the Sandbox already shows: a bound path, or one outside every
        # hidden area. A link to something `_bind_target` refused (the home itself) stays out.
        if any(_inside(target, root) for root in hidden_roots) and not any(
            _inside(target, done) for done in (*bound, *shown)
        ):
            log.warning(
                "Sandbox: not showing %s: its target %s is not in the Sandbox", written, target
            )
            return
        flags.extend(["--ro-bind" if read_only else "--bind", str(target), str(written)])

    resolved: dict[str, Path | None] = {}
    for folder, label in (
        (prefix, "prefix"),
        (install_dir, "game folder"),
        (runner_dir, "runner folder"),
    ):
        target, why = _bind_target(folder, real_home, allow_drive_top=False)
        # Binding the home folder, a folder holding it, or a whole drive after hiding it would
        # put it straight back: the Sandbox would look on and protect nothing (#850). The runner
        # folder comes from PROTONPATH, so the same rule applies to it too.
        if why:
            raise BroadBindError(f"the {label} {folder} is {why}")
        resolved[label] = target
    resolved_prefix = resolved["prefix"]
    bind(resolved_prefix)
    bind(resolved["game folder"])
    bind(resolved["runner folder"], read_only=True)
    alias(prefix)
    alias(install_dir)
    alias(runner_dir, read_only=True)
    # Nadare's config, data and state folders are not bound: the game gets its own prefix and
    # install dir (above) and nothing else of Nadare's. The umu runtime is writable because umu
    # keeps the runtime and its lock files there, but the runtime's bubblewrap, which Nadare runs
    # outside the Sandbox, is bound read-only on top so a game cannot replace it.
    data_home = env.get("XDG_DATA_HOME", "")
    if not data_home or not os.path.isabs(data_home):
        data_home = str(real_home / ".local/share")
    # Older umu builds ignore XDG_DATA_HOME: bind the default place too when they differ.
    umu_places = dict.fromkeys([Path(data_home) / "umu", real_home / ".local/share/umu"])
    owned_base = env.get("UMU_FOLDERS_PATH")
    if owned_base:
        umu_places[Path(owned_base)] = None
    for place in umu_places:
        umu, umu_why = _bind_target(place, real_home, directory=True)
        if umu_why:
            log.warning("Sandbox: not binding the umu runtime %s: %s", place, umu_why)
        bind(umu)
        alias(place)
    for copy in runtime_copies():
        copy_path = _existing(copy)
        if copy_path is not None:
            flags.extend(["--ro-bind", str(copy_path), str(copy_path)])
    cache_root = env.get("XDG_CACHE_HOME")
    cache = Path(cache_root) / paths.app_name() / _CACHE_FOLDER if cache_root else None
    if cache is not None:
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache = None
    cache_target, cache_why = _bind_target(cache, real_home, directory=True)
    if cache_why:
        log.warning("Sandbox: not binding the cache folder %s: %s", cache, cache_why)
    bind(cache_target)
    # The program being started may itself live in a hidden folder (umu-run or a wine build in
    # ~/.local/bin, or anything under the private /tmp): bind it back or bwrap cannot start it.
    programs = argv[:1]
    if argv and Path(argv[0]).name == "mangohud":
        # The overlay starts umu, which may also live in the hidden home or /tmp.
        programs += argv[2:3] if argv[1:2] == ["--dlsym"] else argv[1:2]
    for executable in programs:
        program = _existing(executable) if Path(executable).is_absolute() else None
        if (
            program is not None
            and program.is_file()
            and any(_inside(program, root) for root in hidden_roots)
        ):
            # Nadare's own binary (`nadare --umu-run`, #1341) needs its whole AppImage mount,
            # which lives under /tmp: the one file alone cannot load its bundled libraries.
            appdir = _existing(os.environ.get("APPDIR") or "")
            if appdir is not None and appdir.is_dir() and _inside(program, appdir):
                bind(appdir, read_only=True)
            else:
                bind(program, read_only=True)
        if program is not None and program.is_file():
            alias(executable, read_only=True)

    if "--umu-run" in argv:
        from . import umu as umu_helper

        # The wrapper loads the zipapp after entering the Sandbox. Its interpreter also
        # needs the frozen payload (or the source installation), which can live in the home.
        resources = [umu_helper.managed_path()]
        if getattr(sys, "frozen", False):
            resources.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        else:
            resources.extend(
                [
                    Path(sys.prefix),
                    Path(sys.base_prefix),
                    Path(umu_helper.__file__).resolve().parents[1],
                ]
            )
        for resource in resources:
            target = _existing(resource)
            if target is not None and any(_inside(target, root) for root in hidden_roots):
                target, why = _bind_target(resource, real_home)
                if why:
                    raise BroadBindError(f"the umu helper path {resource} is {why}")
                bind(target, read_only=True)
            alias(resource, read_only=True)

    # Where the Steam Runtime looks for the Steam client's pipe: Nadare's link relay stands in
    # for it, so "Sign in with Google" can still reach the browser (see linkrelay.py).
    if link_pipe is not None:
        flags.extend(["--bind", str(link_pipe), str(real_home / linkrelay.PIPE_RELATIVE)])

    # X11 needs its authority file, which usually sits in the home or the runtime folder.
    xauthority, xauthority_why = _bind_target(env.get("XAUTHORITY"), real_home, regular=True)
    if xauthority_why:
        log.warning("Sandbox: not binding XAUTHORITY %s: %s", env.get("XAUTHORITY"), xauthority_why)
    bind(xauthority, read_only=True)
    # XAUTHORITY is often a link in /tmp to a file in the runtime folder. The private /tmp
    # hides the link, and `bind` above only put the target back: bind it at the path the
    # game was told, too.
    alias(env.get("XAUTHORITY"), read_only=True)

    # Files Nadare downloaded itself (the installer): visible inside the hidden home, never
    # writable by the program that runs them.
    for path in read_only:
        target = _existing(path)
        if target is not None:
            flags.extend(["--ro-bind", str(target), str(target)])
            shown.append(target)
            alias(path, read_only=True)

    wrapped_env = {k: v for k, v in env.items() if k not in _HIDDEN_ENV}
    if cache is not None:
        wrapped_env["XDG_CACHE_HOME"] = str(cache)
    if not host_tmp:
        # A host TMPDIR such as /tmp/foo does not exist in the game's private /tmp; a
        # missing temp folder breaks Wine and some launchers.
        wrapped_env["TMPDIR"] = "/tmp"
    # bwrap itself is dynamically linked and starts before the Sandbox exists: a loader variable
    # from the game's environment would run its library on the host. Pass it to the game only.
    for key, value in list(wrapped_env.items()):
        if _is_loader_env(key):
            flags.extend(["--setenv", key, value])
            del wrapped_env[key]
    return [bwrap, *flags, "--", *argv], wrapped_env


#: Run the program, then wait for Wine's server in the prefix Proton uses; keep the program's exit
#: code. Arguments: the wineserver, Nadare's PID and start time, then the program.
#:
#: While it runs, a watchdog ends the game (``wineserver -k``) once the Nadare process that
#: started it is gone (#1354): ``--watch-bus`` signals only the sandbox's top process, and with
#: ``--share-pids`` Wine's processes live on in Nadare's PID namespace (measured on the rig,
#: 2026-11-15). The start time makes a reused PID never count. Armed only when that process is
#: visible now, so without shared PIDs nothing changes. Plain ``sh``: no awk in every runtime.
_WAIT_FOR_WINE = (
    "w=$1; me=$2; born=$3; shift 3; "
    'p="${STEAM_COMPAT_DATA_PATH:-$WINEPREFIX}/pfx"; '
    'alive() { t=$(cat "/proc/$me/stat" 2>/dev/null) || return 1; t=${t##*) }; '
    'set -- $t; [ "$1" != Z ] && shift 19 && [ "$1" = "$born" ]; }; '
    "dog=; if alive; then "
    '( while alive; do sleep 2; done; WINEPREFIX="$p" "$w" -k ) & dog=$!; fi; '
    '"$@"; s=$?; '
    'WINEPREFIX="$p" "$w" -w; '
    '[ -n "$dog" ] && kill "$dog" 2>/dev/null; exit $s'
)


def process_start_time(pid: int) -> str:
    """The process's start time in clock ticks (``/proc/<pid>/stat`` field 22), or ``""``."""
    return process._pid_starttime(pid) or ""  # noqa: SLF001 - same package


#: The game's ``xdg-open`` inside the Flatpak Sandbox: hand the link to Nadare's relay, in the line
#: format the relay reads from the Steam Runtime (it checks the link before opening anything).
_LINK_SHIM = """#!/bin/sh
printf 'steam://openurl_external/%s\\n' "$1" > "$NADARE_LINK_PIPE"
"""


def _moved_paths(paths_: Iterable[str | Path | None]) -> list[tuple[str, str]]:
    """``(as written, real)`` for each path a symlink leads elsewhere, longest first."""
    moved = []
    for path in paths_:
        if path is None:
            continue
        written = os.path.abspath(os.fspath(path))
        real = _existing(path)
        if real is not None and str(real) != written:
            moved.append((written, str(real)))
    return sorted(moved, key=lambda pair: len(pair[0]), reverse=True)


def _to_real(value: str, moved: list[tuple[str, str]]) -> str:
    """``value`` with a leading written path swapped for its real one.

    Wine's own form of the path (``Z:\\home\\...``, as Endfield's launch helper is given) is
    swapped too; Windows paths compare without case.
    """
    for written, real in moved:
        if value == written or value.startswith(written + os.sep):
            return real + value[len(written) :]
        win = "Z:" + written.replace("/", "\\")
        folded = value.casefold()
        if folded == win.casefold() or folded.startswith(win.casefold() + "\\"):
            return "Z:" + real.replace("/", "\\") + value[len(win) :]
    return value


def _flatpak_spawn() -> str | None:
    """The ``flatpak-spawn`` command, or ``None`` when this is not a Flatpak host.

    Inside a Flatpak the command is part of the runtime, so a missing executable there means
    a broken installation; the literal name still lets a caller try and fail visibly.
    """
    return shutil.which("flatpak-spawn") or ("flatpak-spawn" if sandbox.in_flatpak() else None)


def _expose(
    flags: list[str], exposed: list[Path], path: str | Path | None, *, read_only: bool = False
) -> None:
    """Add ``path`` to the Flatpak sandbox unless it is missing or already exposed."""
    target = _existing(path)
    if target is None or any(_inside(target, done) for done in exposed):
        return
    exposed.append(target)
    option = "--sandbox-expose-path-ro" if read_only else "--sandbox-expose-path"
    flags.append(f"{option}={target}")


def die_with_parent(argv: list[str], *, parent_pid: int | None = None) -> list[str]:
    """``argv`` started so that it is sent SIGTERM when this process dies (#1354).

    The kernel ties the signal to the thread that starts the child; Nadare starts a game from
    a thread that waits for it to end (``process.run``), as bubblewrap's ``--die-with-parent``
    already needs on the AppImage.
    """
    pid = os.getpid() if parent_pid is None else parent_pid
    # ``-m`` works from source and in the frozen binary alike (see diewithparent.py).
    return [sys.executable, "-m", diewithparent.__name__, str(pid), *argv]


def wrap_flatpak(
    argv: list[str],
    env: Mapping[str, str],
    *,
    prefix: Path,
    install_dir: Path | None = None,
    runner_dir: Path | None = None,
    home: Path | None = None,
    read_only: Iterable[Path] = (),
    env_dir: str | Path,
    link_pipe: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Return ``([/bin/sh, -c, ..., flatpak-spawn, ...], env)`` with the home hidden.

    Flatpak has no bubblewrap to run, but ``flatpak-spawn --sandbox`` starts the game with an
    empty home and no ``/mnt``: only the prefix, game folder, runner and installer paths are
    exposed, and the child gets no session bus. The environment is not inherited, so the child's
    copy is written to a private file and read through ``--env-fd``; values never appear in a
    command line. The returned env is the caller's, minus loader variables that would apply to
    the host shell: ``flatpak-spawn`` itself needs the app's session bus to reach the portal.

    ``link_pipe`` is Nadare's link relay (see linkrelay.py). Wine opens a link by running
    ``xdg-open <link>``; without a bus the runtime's own ``xdg-open`` cannot, so a stand-in next
    to the pipe, first on the game's PATH, writes the link to the relay instead.
    """
    real_home = Path(home or Path.home()).resolve()
    for folder, label in (
        (prefix, "prefix"),
        (install_dir, "game folder"),
        (runner_dir, "runner folder"),
    ):
        _target, why = _bind_target(folder, real_home, allow_drive_top=False)
        if why:
            raise BroadBindError(f"the {label} {folder} is {why}")

    wrapped_env = {k: v for k, v in env.items() if k not in _HIDDEN_ENV}
    wrapped_env.pop("XDG_RUNTIME_DIR", None)

    flags: list[str] = [
        "--sandbox",
        "--watch-bus",
        "--share-pids",
        "--sandbox-flag=share-gpu",
        "--sandbox-flag=share-display",
        "--sandbox-flag=share-sound",
        # No ``allow-dbus``: with a session bus the game could ask the Flatpak portal for a new
        # sub-sandbox, which gets the app's full file access (measured: umu's pressure-vessel
        # did exactly that, and the game saw the whole home and /mnt).
    ]
    exposed: list[Path] = []
    # A new game's prefix does not exist before its first launch; a missing path is not exposed,
    # and Proton then could not create it inside the Sandbox (Ubuntu 26.04 VM, 2026-11-14). The
    # folder is Nadare's own and already passed the too-broad check above.
    try:
        Path(prefix).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # exposed only if it exists; Proton then reports the real problem
    _expose(flags, exposed, prefix)
    _expose(flags, exposed, install_dir)
    _expose(flags, exposed, runner_dir, read_only=True)

    cache_root = env.get("XDG_CACHE_HOME")
    cache = Path(cache_root) / paths.app_name() / _CACHE_FOLDER if cache_root else None
    if cache is not None:
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache = None
    cache_target, cache_why = _bind_target(cache, real_home, directory=True)
    if cache_why:
        log.warning("Sandbox: not exposing the cache folder %s: %s", cache, cache_why)
    _expose(flags, exposed, cache_target)
    if cache is not None:
        wrapped_env["XDG_CACHE_HOME"] = str(cache)

    for path in read_only:
        _expose(flags, exposed, path, read_only=True)

    if link_pipe is not None:
        relay_dir = Path(link_pipe).parent
        runtime = env.get("XDG_RUNTIME_DIR")
        if runtime and _inside(relay_dir.resolve(), Path(runtime).resolve()):
            # Measured: Flatpak silently leaves paths under XDG_RUNTIME_DIR out of a sub-sandbox.
            raise ValueError(
                f"the link relay cannot be under {runtime}: Flatpak would not expose it"
            )
        shim = relay_dir / "xdg-open"
        shim.write_text(_LINK_SHIM)
        shim.chmod(0o755)
        _expose(flags, exposed, relay_dir)
        wrapped_env["PATH"] = ":".join(
            part for part in (str(relay_dir), wrapped_env.get("PATH", "")) if part
        )
        wrapped_env["NADARE_LINK_PIPE"] = str(link_pipe)

    # Flatpak exposes the target of a path reached through a symlink (~/Games -> another drive),
    # never the symlink path itself (measured), so the game is told the target paths.
    moved = _moved_paths((prefix, install_dir, runner_dir, *read_only))
    argv = [_to_real(part, moved) for part in argv]
    wrapped_env = {key: _to_real(value, moved) for key, value in wrapped_env.items()}

    # A publisher launcher (NTE's) starts the game and exits; `proton run` then returns, the
    # sandbox ends and the game with it (measured). umu's container waits for every process;
    # here the sandboxed command waits for Wine's server before it ends.
    proton_argv = argv
    if argv and Path(argv[0]).name == "mangohud":
        proton_argv = argv[2:] if argv[1:2] == ["--dlsym"] else argv[1:]
    if runner_dir is not None and len(proton_argv) >= 2 and Path(proton_argv[0]).name == "proton":
        wineserver = _to_real(str(Path(runner_dir) / "files" / "bin" / "wineserver"), moved)
        if proton_argv[1] == "run" and Path(wineserver).is_file():
            me = os.getpid()
            argv = [
                "/bin/sh",
                "-c",
                _WAIT_FOR_WINE,
                "nadare-wait",
                wineserver,
                str(me),
                process_start_time(me) or "0",
                *argv,
            ]

    fd, env_file = tempfile.mkstemp(prefix="nadare-sandbox-env-", dir=env_dir)
    with os.fdopen(fd, "wb") as handle:
        for key, value in wrapped_env.items():
            handle.write(f"{key}={value}\0".encode())

    command = die_with_parent(
        [
            "/bin/sh",
            "-c",
            'exec 3<"$1" || exit 127; rm -f -- "$1"; shift; exec "$@"',
            "nadare-sandbox",
            env_file,
            "flatpak-spawn",
            *flags,
            "--env-fd=3",
            "--",
            *argv,
        ]
    )
    return command, {key: value for key, value in env.items() if not _is_loader_env(key)}


__all__ = [
    "BINARY",
    "DEFAULT_ON",
    "Located",
    "available",
    "die_with_parent",
    "choice",
    "install_hint",
    "locate",
    "ready",
    "runtime_copies",
    "wanted",
    "wrap",
    "wrap_flatpak",
    "works",
]
