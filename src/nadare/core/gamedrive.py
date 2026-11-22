# SPDX-License-Identifier: GPL-3.0-or-later
"""Give a game folder outside ``drive_c`` its own drive letter in the prefix (#1608).

Wine shows a folder outside ``drive_c`` through ``Z:`` (the whole file system), so a game Add Game
placed at ``<library>/<Game>/install`` runs as ``Z:\\home\\...``. Duet Night Abyss's sign-in
program walks the parent folders of that path on its first run, fails on ``Z:\\home`` under Wine
and crashes; the game then waits forever. A ``dosdevices`` link makes the game folder a drive of
its own (Wine picks the deepest drive for a path), so the game runs as ``D:\\EM\\...``.

Only links Nadare can tell are its own are ever changed: one already pointing at the game, one
whose target is gone, or one pointing at another copy of this game (the folder moved). Anything
else is somebody's drive and is left alone; the next free letter is used instead.
"""

from __future__ import annotations

import logging
import os
import string
from pathlib import Path

log = logging.getLogger(__name__)

#: A and B are floppy letters, C is ``drive_c``, S is Proton's game drive and Z is ``/``.
_RESERVED = frozenset("abcsz")


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return False
    return True


def _target(link: Path) -> Path | None:
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    return Path(raw) if os.path.isabs(raw) else link.parent / raw


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _ours(link: Path, exe: str) -> bool:
    """A link Nadare may point elsewhere: its target is gone or holds another copy of the game."""
    target = _target(link)
    if target is None:
        return False
    if not target.exists():
        return True
    return bool(exe) and (target / exe).is_file()


def _point(link: Path, game: Path) -> None:
    tmp = link.with_name(f".{link.name}.nadare-tmp")
    if os.path.lexists(tmp):
        tmp.unlink()
    os.symlink(game, tmp)
    os.replace(tmp, link)


def ensure(prefix: Path, game: Path, preferred: str, *, exe: str = "") -> str | None:
    """Link ``game`` as a drive of ``prefix``; return its letter, or ``None`` when none is needed.

    ``None`` too when the prefix has no ``dosdevices`` yet or every letter belongs to
    something else; the game then runs from ``Z:`` as before.
    """
    prefix, game = Path(prefix), Path(os.path.abspath(game))
    devices = prefix / "dosdevices"
    if not devices.is_dir() or _inside(game, prefix / "drive_c"):
        return None
    letters = [preferred.lower()] + [
        c for c in string.ascii_lowercase if c != preferred.lower() and c not in _RESERVED
    ]
    for letter in letters:
        link = devices / f"{letter}:"
        target = _target(link)
        if target is not None and _same(target, game):
            return letter
    for letter in letters:
        link = devices / f"{letter}:"
        if os.path.lexists(link) and not _ours(link, exe):
            continue
        try:
            _point(link, game)
        except OSError as exc:
            log.warning("could not link %s as drive %s: %s", game, letter.upper(), exc)
            return None
        return letter
    return None
