# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pure core of the Steam per-game prefix fixer.

Some Steam games fail on Linux because something is missing from the game's
own Proton prefix (Epic Seven, app 5019180, needs Microsoft's WebView2
runtime; see ``docs/STEAM-PREFIX-FIX.md``).  This module holds the parts that
can be tested without Steam, Wine, a display or a network: the fix table,
library/prefix discovery, prerequisite detection, the running-game gates, the
backup step, and the plan/run orchestration with an injected installer
runner.

Nothing here launches a process.  The caller supplies
``run_installer(prefix, key)``; the GUI and the real installers come later.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import diskspace, vcredist, webview2
from .backends import BackendError

#: Prerequisite keys; they are also the installer keys handed to ``run_installer``.
KEY_WEBVIEW2 = "webview2"
KEY_VCREDIST = "vcredist"

#: The order prerequisites are reported and fixed in.
PREREQUISITE_KEYS: tuple[str, ...] = (KEY_WEBVIEW2, KEY_VCREDIST)

_LABELS = {
    KEY_WEBVIEW2: "Microsoft WebView2",
    KEY_VCREDIST: "Visual C++ runtime",
}


@dataclass(frozen=True)
class FixRow:
    """One game's known fix.  Adding a game is one row in :data:`FIXES`."""

    app_id: str
    game: str
    needs: tuple[str, ...]
    #: ``False`` while the row is only expected to need this (GFL2 on Steam).
    verified: bool = True
    note: str = ""


#: The fix table (data, not code).
FIXES: tuple[FixRow, ...] = (
    FixRow("5019180", "Epic Seven", (KEY_WEBVIEW2,)),
    FixRow(
        "3308670",
        "Girls' Frontline 2: Exilium (Haoplay)",
        (KEY_WEBVIEW2,),
        verified=False,
        note="to verify on the Steam build",
    ),
    FixRow(
        "3347400",
        "Girls' Frontline 2: Exilium (Darkwinter)",
        (KEY_WEBVIEW2,),
        verified=False,
        note="region-locked on the test account; to verify on the Steam build",
    ),
)


def fix_for(app_id: str | int) -> FixRow | None:
    """The table row for an exact app id, or ``None`` (no prefix/suffix matching)."""
    key = str(app_id).strip()
    if not key:
        return None
    return next((row for row in FIXES if row.app_id == key), None)


# ---- Steam libraries and the game's prefix -----------------------------

#: Steam's ``steamapps`` folders, native then Flatpak.  Kept in step with
#: ``backends/steam.py``; duplicated here because ``core`` must not import
#: ``backends`` (``tests/test_layering.py``).
_STEAMAPPS_ROOTS = (
    Path(".local") / "share" / "Steam" / "steamapps",
    Path(".steam") / "steam" / "steamapps",
    Path(".var") / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "steamapps",
)

_LIBRARY_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')


def steamapps_dirs() -> list[Path]:
    """Every plausible ``steamapps`` directory, including extra library folders."""
    home = Path(os.path.expanduser("~"))
    roots = [home / relative for relative in _STEAMAPPS_ROOTS]
    for root in list(roots):
        try:
            text = (root / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _LIBRARY_PATH_RE.finditer(text):
            library = Path(match.group(1).replace("\\\\", "\\")).expanduser()
            if library.is_absolute():
                roots.append(library / "steamapps")
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen:
            continue
        seen.add(key)
        found.append(root)
    return found


#: The parameter of :func:`find_library` is also called ``steamapps_dirs``; this
#: alias keeps the default call unambiguous.
_discover_steamapps = steamapps_dirs


def _flatpak_unresolved() -> bool:
    """Flatpak Steam is installed but its library path cannot be read.

    The native and Flatpak layouts differ, and a sandboxed client can leave a
    path that exists only inside its own mount namespace.  That is a different
    problem from "the game is not installed", so it gets its own message.
    """
    app = Path(os.path.expanduser("~")) / ".var" / "app" / "com.valvesoftware.Steam"
    steamapps = app / ".local" / "share" / "Steam" / "steamapps"
    return app.is_dir() and not steamapps.is_dir()


def find_library(
    app_id: str | int, *, steamapps_dirs: Iterable[Path] | None = None
) -> Path:
    """The ``steamapps`` directory holding ``appmanifest_<id>.acf``.

    Raises :class:`BackendError` with a plain reason when the game is not
    installed (or when Flatpak Steam's library cannot be resolved).
    """
    key = str(app_id).strip()
    if not key.isdigit():
        raise BackendError(f"Steam app id must be numeric, got {key!r}")
    dirs = list(steamapps_dirs) if steamapps_dirs is not None else _discover_steamapps()
    manifest = f"appmanifest_{key}.acf"
    for steamapps in dirs:
        if (Path(steamapps) / manifest).is_file():
            return Path(steamapps)
    if _flatpak_unresolved():
        raise BackendError(
            "Flatpak Steam's library path could not be resolved, so Nadare cannot find "
            f"{manifest}. Point the game's Steam library at a readable folder, or install "
            "Steam natively."
        )
    raise BackendError(
        f"Steam app {key} is not installed: no {manifest} in any Steam library. "
        "Install it in Steam first."
    )


@dataclass(frozen=True)
class GameLocation:
    library: Path
    manifest: Path
    prefix: Path


def locate(app_id: str | int, *, steamapps_dirs: Iterable[Path] | None = None) -> GameLocation:
    """The game, its library and its Proton prefix (which must already exist)."""
    key = str(app_id).strip()
    library = Path(find_library(key, steamapps_dirs=steamapps_dirs))
    prefix = library / "compatdata" / key / "pfx"
    if not prefix.is_dir():
        raise BackendError(
            f"Steam app {key} has no Proton prefix yet ({prefix}). Run the game once "
            "from Steam and close it (the error it shows is expected), then try again."
        )
    return GameLocation(library=library, manifest=library / f"appmanifest_{key}.acf", prefix=prefix)


# ---- prerequisites -----------------------------------------------------


@dataclass(frozen=True)
class Prerequisite:
    key: str
    label: str
    present: bool


def check_prerequisites(prefix: Path) -> list[Prerequisite]:
    """WebView2 and the real (not Wine-builtin) VC++ runtime, in table order."""
    prefix = Path(prefix)
    checks: dict[str, Callable[[Path], bool]] = {
        KEY_WEBVIEW2: lambda prefix: webview2.installed(prefix) and webview2.registered(prefix),
        KEY_VCREDIST: vcredist.installed,
    }
    return [
        Prerequisite(key, _LABELS[key], bool(checks[key](prefix))) for key in PREREQUISITE_KEYS
    ]


# ---- running gates -----------------------------------------------------


@dataclass(frozen=True)
class Running:
    pid: int
    what: str  # "game" | "wineserver"


def _argv(entry: Path) -> list[str] | None:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _environ(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep:
            out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    def normal(value: str | os.PathLike[str]) -> str:
        try:
            return str(Path(value).expanduser().resolve())
        except OSError:
            return os.path.normpath(str(value))

    return normal(left) == normal(right)


def _game_argv(argv: list[str], app_id: str) -> bool:
    """Same signals as the Steam backend: the URI, ``AppId=`` and ``-applaunch <id>``."""
    if f"steam://rungameid/{app_id}" in argv or f"AppId={app_id}" in argv:
        return True
    return any(
        token == "-applaunch" and argv[i + 1] == app_id for i, token in enumerate(argv[:-1])
    )


def running_blockers(
    app_id: str | int,
    prefix: Path | None,
    *,
    proc_root: str | os.PathLike[str] = "/proc",
) -> list[Running]:
    """Processes that make editing the prefix unsafe: the game or its wineserver.

    ``prefix=None`` looks for the game only. ``proc_root`` is injectable so tests use a
    fake ``/proc``.  Other users' processes, our own process and entries that vanish
    mid-read are ignored.
    """
    key = str(app_id).strip()
    me = os.getpid()
    uid = os.getuid()
    hits: list[Running] = []
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return hits
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = _argv(entry)
        if not argv:
            continue
        if key and _game_argv(argv, key):
            hits.append(Running(int(entry.name), "game"))
            continue
        if prefix is not None and Path(argv[0]).name == "wineserver":
            env_prefix = _environ(entry).get("WINEPREFIX")
            if env_prefix and _same_path(env_prefix, prefix):
                hits.append(Running(int(entry.name), "wineserver"))
    return hits


def game_running(
    app_id: str | int,
    *,
    proc_root: str | os.PathLike[str] = "/proc",
) -> bool:
    """Whether the Steam game itself is running (its wineserver does not count)."""
    return bool(running_blockers(app_id, None, proc_root=proc_root))


def _blockers_message(hits: list[Running]) -> str:
    what = "the game" if hits[0].what == "game" else "a wineserver for this prefix"
    return (
        f"{what} is still running (PID {hits[0].pid}), so the prefix cannot be changed. "
        "Close the game (and Steam, if it keeps it open) and try again."
    )


# ---- backup ------------------------------------------------------------


def prefix_size(prefix: Path) -> int:
    """Total bytes of the regular files under ``prefix`` (symlinks are not followed)."""
    total = 0
    for root, _dirs, files in os.walk(prefix, onerror=lambda _e: None):
        for name in files:
            try:
                path = Path(root) / name
                if path.is_symlink():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
    return total


def plan_backup(compatdata: Path, app_id: str | int, fix_key: str) -> Path:
    """A fresh ``<id>.bak-pre-<fix>`` path; an existing backup is never reused."""
    compatdata = Path(compatdata)
    base = f"{str(app_id).strip()}.bak-pre-{fix_key}"
    candidate = compatdata / base
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = compatdata / f"{base}-{suffix}"
        suffix += 1
    return candidate


@dataclass(frozen=True)
class Backup:
    source: Path
    dest: Path
    size_bytes: int


def make_backup(prefix: Path, dest: Path) -> Backup:
    """Copy ``prefix`` to ``dest``; refuse if ``dest`` already exists.

    The copy is staged under a temporary sibling name and only renamed to
    ``dest`` when complete, so an interrupted copy never looks like a usable
    backup.  Nothing is ever deleted.
    """
    prefix, dest = Path(prefix), Path(dest)
    if dest.exists() or dest.is_symlink():
        raise BackendError(f"refusing to overwrite the existing backup {dest}")
    if not prefix.is_dir():
        raise BackendError(f"the prefix {prefix} does not exist, so there is nothing to back up")
    size = prefix_size(prefix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    diskspace.check_space(dest.parent, size)  # a copy that fills the disk breaks Steam too
    staging = dest.with_name(f".{dest.name}.partial-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        shutil.copytree(prefix, staging, symlinks=True)
        staging.rename(dest)
    except OSError as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise BackendError(f"could not back up the prefix to {dest}: {exc}") from exc
    return Backup(source=prefix, dest=dest, size_bytes=size)


# ---- plan / run --------------------------------------------------------


@dataclass(frozen=True)
class Step:
    action: str  # "backup" | "install"
    text: str
    installer: str | None = None


@dataclass(frozen=True)
class FixPlan:
    app_id: str
    game: str
    library: Path
    prefix: Path
    prerequisites: tuple[Prerequisite, ...]
    missing: tuple[str, ...]
    fix_key: str
    backup: Path
    backup_size: int
    steps: tuple[Step, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.missing


def plan_fix(
    app_id: str | int,
    *,
    steamapps_dirs: Iterable[Path] | None = None,
    proc_root: str | os.PathLike[str] = "/proc",
) -> FixPlan:
    """Work out everything the fix would do, without changing anything.

    Only the row's own ``needs`` are fixed; other missing prerequisites are
    reported (in ``prerequisites``) but not installed, because the fix table
    says what the tested fix is.  Raises :class:`BackendError` for an unknown
    or unverified game, a missing game/prefix, or a running game.
    """
    row = fix_for(app_id)
    if row is None:
        raise BackendError(f"no known fix for Steam app {app_id}")
    if not row.verified:
        raise BackendError(
            f"the fix for {row.game} (app {row.app_id}) is not verified yet"
            + (f" ({row.note})" if row.note else "")
            + "; it is listed only as an expectation"
        )
    location = locate(row.app_id, steamapps_dirs=steamapps_dirs)
    blockers = running_blockers(row.app_id, location.prefix, proc_root=proc_root)
    if blockers:
        raise BackendError(_blockers_message(blockers))

    prerequisites = check_prerequisites(location.prefix)
    present = {item.key: item.present for item in prerequisites}
    missing = tuple(key for key in row.needs if not present.get(key, False))
    warnings = tuple(
        f"{item.label} is missing but is not part of this game's fix"
        for item in prerequisites
        if not item.present and item.key not in row.needs
    )
    if missing:
        fix_key = "-".join(missing)
        backup = plan_backup(location.prefix.parent, row.app_id, fix_key)
        size = prefix_size(location.prefix)
        steps = [
            Step("backup", f"Back up the prefix to {backup} ({size} bytes)"),
            *(
                Step("install", f"Install {_LABELS[key]} into the prefix", installer=key)
                for key in missing
            ),
        ]
    else:
        fix_key = ""
        backup = plan_backup(location.prefix.parent, row.app_id, "nothing")
        size = 0
        steps = []
    return FixPlan(
        app_id=row.app_id,
        game=row.game,
        library=location.library,
        prefix=location.prefix,
        prerequisites=tuple(prerequisites),
        missing=missing,
        fix_key=fix_key,
        backup=backup,
        backup_size=size,
        steps=tuple(steps),
        warnings=warnings,
    )


@dataclass(frozen=True)
class FixResult:
    ok: bool
    line: str
    backups: tuple[Path, ...]
    installed: tuple[str, ...]
    failed: str | None = None


def run_fix(
    plan: FixPlan,
    *,
    run_installer: Callable[[Path, str], object],
    backup_fn: Callable[[Path, Path], Backup] = make_backup,
    emit: Callable[[str], None] | None = None,
) -> FixResult:
    """Back up the prefix, then run each installer; stop at the first failure.

    ``run_installer(prefix, key)`` is injected (the real one downloads and
    runs the installer on a private display).  It may raise or return a
    non-zero exit code; both stop the run.  The result carries a plain line
    for the UI and never removes a backup.
    """

    def report(message: str) -> None:
        if emit is not None:
            emit(message)

    if plan.complete:
        line = f"{plan.game}: nothing to do; the fix is already in place."
        report(line)
        return FixResult(True, line, (), ())

    dest = plan.backup
    if dest.exists() or dest.is_symlink():
        # A backup appeared between planning and running: never overwrite it.
        dest = plan_backup(dest.parent, plan.app_id, plan.fix_key)
    report(f"{plan.game}: backing up the prefix to {dest} ({plan.backup_size} bytes)…")
    try:
        backup = backup_fn(plan.prefix, dest)
    except Exception as exc:  # noqa: BLE001 - reported as a plain line
        line = f"{plan.game}: could not back up the prefix ({exc}); nothing was changed."
        report(line)
        return FixResult(False, line, (), (), "backup")

    installed: list[str] = []
    for step in plan.steps:
        if step.action != "install" or step.installer is None:
            continue
        key = step.installer
        label = _LABELS.get(key, key)
        report(f"{plan.game}: installing {label}…")
        try:
            code = run_installer(plan.prefix, key)
        except Exception as exc:  # noqa: BLE001 - reported as a plain line
            line = (
                f"{plan.game}: failed while installing {label} ({exc}). "
                f"The prefix backup is at {backup.dest}."
            )
            report(line)
            return FixResult(False, line, (backup.dest,), tuple(installed), key)
        if isinstance(code, int) and code != 0:
            line = (
                f"{plan.game}: failed while installing {label} (installer exit code {code}). "
                f"The prefix backup is at {backup.dest}."
            )
            report(line)
            return FixResult(False, line, (backup.dest,), tuple(installed), key)
        installed.append(key)

    names = ", ".join(_LABELS.get(key, key) for key in installed)
    line = (
        f"{plan.game}: fixed; installed {names}. "
        f"The prefix backup is at {backup.dest} (kept until you delete it)."
    )
    report(line)
    return FixResult(True, line, (backup.dest,), tuple(installed))


__all__ = [
    "FIXES",
    "KEY_VCREDIST",
    "KEY_WEBVIEW2",
    "PREREQUISITE_KEYS",
    "Backup",
    "FixPlan",
    "FixResult",
    "FixRow",
    "GameLocation",
    "Prerequisite",
    "Running",
    "Step",
    "check_prerequisites",
    "find_library",
    "fix_for",
    "locate",
    "make_backup",
    "plan_backup",
    "plan_fix",
    "prefix_size",
    "run_fix",
    "running_blockers",
    "steamapps_dirs",
]
