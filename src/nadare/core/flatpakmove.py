# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Move the AppImage's games and settings into the Flatpak's folder, once (#1353).

The Flatpak has no home access (#1244), so a player who switches would start empty. The
AppImage (outside the Flatpak) renames its data folder and config folder into
``~/.var/app/<id>/``, where the Flatpak reads them, and rewrites the stored paths that pointed
into them. A rename, never a copy: refused across filesystems, so 100 GB is never copied
silently. Refused while a game or the Flatpak runs, when the Flatpak already has games (no
merge), with another unfinished move, and inside the Flatpak. A Flatpak folder without games
(it was opened once) is renamed aside, never deleted.

Order, so a crash leaves something the next start can finish or forget (a journal in the old
config folder says which): journal, rename the data folder, write the rewritten config into the
Flatpak folder, rename the old ``config.json`` aside (never deleted) and leave a note, clear
the journal. Keyring entries need nothing: both builds use the same Secret Service.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import location, paths, process, relocate, runlock, sandbox
from .flatpakfs import APP_ID

log = logging.getLogger(__name__)

JOURNAL = ".move-to-flatpak.json"
RETIRED = "config.json.moved-to-flatpak"
NOTE = "MOVED-TO-FLATPAK.txt"
#: Folders the Flatpak can see by default besides its own (packaging/flatpak manifest).
_VISIBLE = ("Games", ".var/app/com.valvesoftware.Steam", ".local/share/Steam", ".steam")
_VISIBLE_ROOTS = ("/mnt", "/media", "/run/media")

Runner = Callable[..., subprocess.CompletedProcess]


class MoveRefused(RuntimeError):
    """The move cannot run now; the message says why."""


def targets(home: Path | None = None) -> tuple[Path, Path]:
    """``(data, config)`` folders the Flatpak uses for Nadare."""
    base = Path(home or Path.home()) / ".var" / "app" / APP_ID
    return base / "data" / paths.APP, base / "config" / paths.APP


def _flatpak(run: Runner, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return run(["flatpak", *args], capture_output=True, text=True, timeout=20,
                   env=process.child_environ())
    except (OSError, subprocess.SubprocessError):
        return None


def installed(run: Runner = subprocess.run) -> bool:
    out = _flatpak(run, "info", APP_ID)
    return out is not None and out.returncode == 0


def _flatpak_running(run: Runner) -> bool | None:
    """Whether the Nadare Flatpak runs; ``None`` when ``flatpak ps`` could not tell (#1479)."""
    out = _flatpak(run, "ps", "--columns=application")
    if out is None or out.returncode != 0:
        return None
    return APP_ID in (out.stdout or "").split()


def probably_installed(home: Path | None = None) -> bool:
    """A quick look (no subprocess) for the Flatpak's install folder, user or system wide."""
    if sandbox.in_flatpak() or paths.app_name() != paths.APP:
        return False
    user = Path(home or Path.home()) / ".local" / "share" / "flatpak" / "app" / APP_ID
    return user.is_dir() or (Path("/var/lib/flatpak/app") / APP_ID).is_dir()


def available(run: Runner = subprocess.run) -> bool:
    """Whether to offer the move: an AppImage/source build on a host with the Flatpak."""
    return not sandbox.in_flatpak() and paths.app_name() == paths.APP and installed(run)


def _has_games(data: Path, config: Path) -> bool | None:
    """Whether the Flatpak's own setup has games; ``None`` when it could not be read.

    A missing folder or config is a proven empty setup. An unreadable folder or a damaged
    config is not: the move would rename it aside on a guess, so it is refused (#1479).
    """
    try:
        if any((data / "profiles").iterdir()):
            return True
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        raw = json.loads((config / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    profiles = raw.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        return None
    return bool(profiles)


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _same_filesystem(src: Path, dst: Path) -> bool:
    try:
        return os.stat(src).st_dev == os.stat(_nearest_existing(dst)).st_dev
    except OSError:
        return False


def _game_prefix(name: str, profile: dict) -> str | None:
    """The prefix a game uses: the one it names, else ``profiles/<name>/prefix`` if present."""
    for key in ("prefix", "wineprefix"):
        prefix = location._setting(profile, key)  # noqa: SLF001 - the profile reader
        if prefix:
            return os.path.expanduser(str(prefix))
    default = paths.profiles_dir() / name / "prefix"
    return str(default) if default.is_dir() else None


def _running_games(config, proc_root: str | os.PathLike[str] = "/proc") -> list[str] | None:
    """What runs in a prefix the move would rename, or ``None`` when that cannot be told.

    The same rule as Remove (C3, 2026-11-21): Wine processes in a game's prefix (started by
    Nadare or not: a desktop entry, Steam, a leftover wineserver) and Nadare run locks, plus any
    Wine process whose prefix lies in Nadare's data folder, which moves whole.
    """
    found: list[str] = []
    try:
        scan = process.ProcessScanRound(proc_root)
        for name, profile in config.profiles.items():
            if not isinstance(profile, dict):
                continue
            prefix = _game_prefix(str(name), profile)
            if prefix and (runlock.running(prefix) or process.prefix_pids(prefix, scan=scan)):
                found.append(str(profile.get("title") or name))
        data = os.path.realpath(paths.data_dir())
        for record in scan.records():
            values = (record.environ.get(k) for k in ("WINEPREFIX", "STEAM_COMPAT_DATA_PATH"))
            if any(v and process._under_prefix(os.path.realpath(v), data) for v in values):  # noqa: SLF001
                if not found:
                    found.append("A Windows program in Nadare's data folder")
                break
    except Exception:  # noqa: BLE001 - unknown means do not move
        log.warning("could not check for running games before the Flatpak move", exc_info=True)
        return None
    return found


def refusal(
    config,
    *,
    home: Path | None = None,
    run: Runner = subprocess.run,
    proc_root: str | os.PathLike[str] = "/proc",
) -> str | None:
    """Why the move cannot run now, or ``None``."""
    if sandbox.in_flatpak():
        return "This is the Flatpak already; run the move from the AppImage."
    if paths.app_name() != paths.APP:
        return "Only the stable Nadare can move to the Flatpak."
    if not installed(run):
        return "The Nadare Flatpak is not installed."
    running = _flatpak_running(run)
    if running is None:
        return ("Nadare could not check whether the Nadare Flatpak is running (flatpak ps "
                "failed). Close it and try again. Nothing was changed.")
    if running:
        return "The Nadare Flatpak is running. Close it first."
    games = _running_games(config, proc_root)
    if games is None:
        return ("Nadare could not check whether a game is running. Close your games and try "
                "again. Nothing was changed.")
    if games:
        return f"{', '.join(games)} is running. Close it first."
    data, conf = paths.data_dir(), paths.config_dir()
    if (conf / JOURNAL).exists():
        return "An earlier move to the Flatpak is unfinished; restart Nadare to finish it."
    if any(conf.glob(".*.importmove.json")):
        return "A move into a library is unfinished; finish it first (restart Nadare)."
    new_data, new_conf = targets(home)
    has_games = _has_games(new_data, new_conf)
    if has_games is None:
        return (f"Nadare could not read the Flatpak's own setup ({new_conf.parent}); open the "
                "Flatpak once or fix that folder, then try again. Nothing was changed.")
    if has_games:
        return "The Flatpak already has games of its own; Nadare does not merge two setups."
    for src, dst in ((data, new_data), (conf, new_conf)):
        if src.exists() and not _same_filesystem(src, dst.parent):
            return (f"{src} and {dst.parent} are on different drives; a move would copy "
                    "everything. Nothing was changed.")
    return None


def _step(name: str, action: Callable[[], object]) -> None:
    """One step of the move (a seam for the crash tests)."""
    action()


def _aside(folder: Path) -> None:
    """Rename a Flatpak folder that has no games beside it (never deleted)."""
    try:
        if not folder.is_dir() or not any(folder.iterdir()):
            if folder.is_dir():
                folder.rmdir()  # empty: nothing to keep
            return
    except OSError:
        return
    target = folder.with_name(f"{folder.name}.before-move-{time.strftime('%Y%m%d-%H%M%S')}")
    os.rename(folder, target)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _rewritten(data: dict[str, Any], journal: dict[str, str]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    relocate._repoint(out, Path(journal["data_from"]), Path(journal["data_to"]))  # noqa: SLF001
    relocate._repoint(out, Path(journal["config_from"]), Path(journal["config_to"]))  # noqa: SLF001
    return out


def _write_config(journal: dict[str, str], data: dict[str, Any]) -> None:
    from .config import Config  # config imports location, which imports this package

    target = Config(path=Path(journal["config_to"]) / "config.json", data=_rewritten(data, journal))
    target.save()


def _retire(journal: dict[str, str]) -> None:
    old = Path(journal["config_from"])
    if (old / "config.json").exists():
        os.rename(old / "config.json", old / RETIRED)
    (old / NOTE).write_text(
        "Nadare's games and settings moved to the Flatpak:\n"
        f"  {journal['data_to']}\n  {journal['config_to']}\n"
        f"The old settings file is kept as {old / RETIRED}.\n",
        encoding="utf-8",
    )


def _unreachable(config_data: dict[str, Any], home: Path) -> list[str]:
    """Games whose folders the Flatpak cannot see by default (after the move)."""
    roots = [home / ".var" / "app" / APP_ID, *(home / rel for rel in _VISIBLE),
             *(Path(r) for r in _VISIBLE_ROOTS)]
    notes = []
    profiles = config_data.get("profiles") or {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        for key in ("install_dir", "prefix"):
            value = location._setting(profile, key)  # noqa: SLF001
            if not value:
                continue
            path = Path(os.path.expanduser(value))
            if not any(path == r or r in path.parents for r in roots):
                title = profile.get("title") or name
                notes.append(
                    f"{title} lives in {path}, which the Flatpak cannot see. Allow it with: "
                    f"flatpak override --user --filesystem={path} {APP_ID}"
                )
                break
    return notes


def move(config, *, home: Path | None = None, run: Runner = subprocess.run) -> list[str]:
    """Move everything; raise :class:`MoveRefused` with the reason when it cannot run.

    Afterwards ``config`` points at the Flatpak's settings file, so a later save in this
    process can never recreate the old one with moved-away paths. Returns notes for the player.
    """
    home = Path(home or Path.home())
    with config.locked():
        why = refusal(config, home=home, run=run)
        if why:
            raise MoveRefused(why)
        data, conf = paths.data_dir(), paths.config_dir()
        new_data, new_conf = targets(home)
        journal = {
            "data_from": str(data), "data_to": str(new_data),
            "config_from": str(conf), "config_to": str(new_conf),
        }
        current = copy.deepcopy(config.data)
        _aside(new_data)
        _aside(new_conf)
        _step("journal", lambda: _write_json(conf / JOURNAL, journal))

        def rename_data() -> None:
            if data.exists():
                new_data.parent.mkdir(parents=True, exist_ok=True)
                os.rename(data, new_data)

        _step("data", rename_data)
        _step("config", lambda: _write_config(journal, current))
        _step("retire", lambda: _retire(journal))
        (conf / JOURNAL).unlink(missing_ok=True)
        config.path = new_conf / "config.json"
        config.data = _rewritten(current, journal)
    return [
        f"Moved your games and settings to the Flatpak ({new_data.parent.parent}).",
        *_unreachable(config.data, home),
    ]


def finish_interrupted(*, home: Path | None = None) -> list[str]:
    """Finish or forget a move a crash interrupted (start-up, before the config is read)."""
    conf = paths.config_dir()
    path = conf / JOURNAL
    if not path.exists():
        return []
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
        keys = ("data_from", "data_to", "config_from", "config_to")
        if not isinstance(journal, dict) or not all(isinstance(journal.get(k), str)
                                                    for k in keys):
            raise ValueError
    except (OSError, ValueError):
        return [f"An interrupted move to the Flatpak could not be read; {path} was kept."]
    data, new_data = Path(journal["data_from"]), Path(journal["data_to"])
    if data.exists() and new_data.exists():
        return [f"Both {data} and {new_data} exist after an interrupted move to the Flatpak; "
                f"nothing was changed and {path} was kept."]
    if data.exists():
        path.unlink(missing_ok=True)
        return ["A move to the Flatpak had not started; nothing changed."]
    new_conf = Path(journal["config_to"])
    if not (new_conf / "config.json").exists():
        try:
            current = json.loads((conf / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [f"An interrupted move to the Flatpak has no settings to finish with; "
                    f"{path} was kept."]
        _write_config(journal, current)
    _retire(journal)
    path.unlink(missing_ok=True)
    return [f"Finished moving your games and settings to the Flatpak ({new_data})."]


def moved_note(*, home: Path | None = None) -> str | None:
    """Shown by the AppImage after the move: where the games went."""
    conf = paths.config_dir()
    if (conf / "config.json").exists() or not (conf / NOTE).exists():
        return None
    new_data, _ = targets(home)
    return ("Your games and settings moved to the Nadare Flatpak "
            f"({new_data.parent.parent}). Open Nadare from the Flatpak to play them.")


__all__ = [
    "MoveRefused", "available", "finish_interrupted", "installed", "move", "moved_note",
    "probably_installed", "refusal", "targets",
]
