# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""One read-only view of every storage operation that did not finish (#1372 phase 3).

Four records can say so, each settled by its own code at startup or on a retry:

- ``removal_pending`` (:mod:`removal`): Uninstall recorded the folders it was deleting;
- ``move_originals_pending`` (:mod:`relocate`): a Move still has an original to delete;
- ``move_in_progress`` (:mod:`relocate`): a Move a crash interrupted;
- the import-move journal next to the config (:mod:`importmove`).

This module only reads them (and asks whether a named folder still exists), for the support
report. It never acts, prunes or rewrites: the stored keys stay as b24 reads them
(DECISIONS 2026-11-15). A damaged record is listed as damaged, never raised.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REMOVAL = "removal_pending"  # removal._PENDING_REMOVAL
ORIGINALS = "move_originals_pending"  # relocate.PENDING_ORIGINALS
IN_PROGRESS = "move_in_progress"  # relocate.MOVE_IN_PROGRESS

#: Shown in the report: what the player pressed, not the record's name.
LABELS = {
    "removal": "Uninstall",
    "move-original": "Move (original still to delete)",
    "move": "Move",
    "import-move": "Move into a library",
}


@dataclass(frozen=True)
class Unfinished:
    profile: str
    kind: str  # one of LABELS
    detail: str


def import_journal_path(config_path: str | Path) -> Path:
    """Where :mod:`importmove` keeps its journal for the config at ``config_path``."""
    path = Path(os.path.realpath(config_path))
    return path.with_name(f".{path.name}.importmove.json")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() and "\x00" not in value else None


def _where(value: Any) -> str:
    text = _text(value)
    if text is None:
        return "(no folder)"
    try:
        there = os.path.lexists(os.path.expanduser(text))
    except (OSError, ValueError):
        there = False
    return f"{text} ({'still there' if there else 'gone'})"


def removal_leftovers(profile: Mapping[str, Any]) -> list[str]:
    """The folders an interrupted Uninstall recorded that are still on disk (r3b01 F1).

    Read only, like the rest of this module: the game page and ``nadare status`` use it to say
    the Uninstall did not finish. A missing or damaged record gives an empty list.
    """
    record = profile.get(REMOVAL) if isinstance(profile, Mapping) else None
    if not isinstance(record, Mapping):
        return []
    found: list[str] = []
    for _key, value in sorted(record.items(), key=lambda item: str(item[0])):
        text = _text(value)
        if text is None:
            continue
        try:
            there = os.path.lexists(os.path.expanduser(text))
        except (OSError, ValueError):
            there = False
        if there and text not in found:
            found.append(text)
    return found


def _removal(name: str, record: Any) -> list[Unfinished]:
    if not isinstance(record, Mapping) or not record:
        return [Unfinished(name, "removal", "damaged record")]
    parts = [f"{key}: {_where(value)}" for key, value in sorted(record.items(), key=str)]
    return [Unfinished(name, "removal", "; ".join(parts))]


def _originals(name: str, record: Any) -> list[Unfinished]:
    if not isinstance(record, list):
        return [Unfinished(name, "move-original", "damaged record")]
    found = []
    for entry in record:
        if (isinstance(entry, (list, tuple)) and len(entry) in (2, 4)
                and _text(entry[0]) and _text(entry[1])):
            note = "" if len(entry) == 4 else " (no folder identity: never deleted)"
            found.append(Unfinished(
                name, "move-original",
                f"original {_where(entry[0])}, copy {_where(entry[1])}{note}",
            ))
        else:
            found.append(Unfinished(name, "move-original", "damaged record"))
    return found


def _in_progress(name: str, record: Any) -> list[Unfinished]:
    if not isinstance(record, Mapping) or not (_text(record.get("from"))
                                               and _text(record.get("to"))):
        return [Unfinished(name, "move", "damaged record")]
    return [Unfinished(
        name, "move", f"from {_where(record['from'])} to {_where(record['to'])}"
    )]


def _journal(path: Path) -> list[Unfinished]:
    try:
        if not os.path.lexists(path):
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [Unfinished("", "import-move", f"damaged record {path}")]
    if not isinstance(raw, Mapping):
        return [Unfinished("", "import-move", f"damaged record {path}")]
    name = raw.get("profile") if isinstance(raw.get("profile"), str) else ""
    library = _text(raw.get("library"))
    if not name or library is None:
        return [Unfinished(name, "import-move", f"damaged record {path}")]
    trees = raw.get("trees")
    count = len(trees) if isinstance(trees, list) else 0
    folders = "folder" if count == 1 else "folders"
    return [Unfinished(
        name, "import-move", f"into {_where(library)}, {count} {folders}; record {path}"
    )]


def unfinished(profiles: Any, journal: str | Path | None = None) -> list[Unfinished]:
    """Every unfinished storage operation in ``profiles`` and the import-move ``journal``."""
    found: list[Unfinished] = []
    if isinstance(profiles, Mapping):
        for name, profile in profiles.items():
            if not isinstance(profile, Mapping):
                continue
            name = str(name)
            if profile.get(REMOVAL) is not None:
                found += _removal(name, profile[REMOVAL])
            if profile.get(ORIGINALS) is not None:
                found += _originals(name, profile[ORIGINALS])
            if profile.get(IN_PROGRESS) is not None:
                found += _in_progress(name, profile[IN_PROGRESS])
    if journal is not None:
        found += _journal(Path(journal))
    return found


__all__ = ["LABELS", "Unfinished", "import_journal_path", "unfinished"]
