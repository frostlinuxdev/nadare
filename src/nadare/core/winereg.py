# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Small edits to a Wine prefix's ``user.reg`` text, without starting Wine.

Call these only while the prefix's wineserver is not running: a running wineserver keeps the
registry in memory and writes it back over the file.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import atomicfile


def _line_ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def set_user_value(prefix: Path | str, key: str, name: str, value: str) -> bool:
    """Set the string value ``name`` under ``HKCU\\<key>`` in ``prefix/user.reg``.

    ``key`` uses single backslashes (``Software\\Wine``). Returns True if the file changed;
    False if it already held the value or ``user.reg`` does not exist yet.
    """
    path = Path(prefix) / "user.reg"
    if not path.exists():
        return False
    escaped = key.replace("\\", "\\\\")
    header = f"[{escaped}]"
    entry = f'"{name}"="{value}"'
    prefix_of_entry = f'"{name}"='
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)

    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith(header)
            and line[len(header) : len(header) + 1] in ("", " ", "\r", "\n")
        ),
        None,
    )
    if start is None:
        stamp = int(time.time())
        filetime = (stamp + 11644473600) * 10_000_000
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n{header} {stamp}\n#time={filetime:x}\n{entry}\n"
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines)
        )
        found = next(
            (i for i in range(start + 1, end) if lines[i].lstrip().startswith(prefix_of_entry)),
            None,
        )
        if found is not None:
            if lines[found].rstrip("\r\n") == entry:
                return False
            lines[found] = entry + _line_ending(lines[found])
        else:
            insert_at = start + 1
            if insert_at < len(lines) and lines[insert_at].startswith("#time="):
                insert_at += 1
            lines.insert(insert_at, entry + (_line_ending(lines[start]) or "\n"))
        text = "".join(lines)
    atomicfile.write(path, text)
    return True


def set_app_version(prefix: Path | str, exe: str, version: str) -> bool:
    """Make Wine report Windows ``version`` (``win7``, ``win10``…) to ``exe`` only."""
    return set_user_value(prefix, f"Software\\Wine\\AppDefaults\\{exe}", "Version", version)
