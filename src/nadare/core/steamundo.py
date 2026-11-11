# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep the undo of a Set up change on disk, per game, so it outlives the window.

Only the two steps that change a Steam setting can be undone: the runner (``config.vdf``) and the
launch options (``localconfig.vdf``). What is saved is exactly what that undo needs.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import paths, steamtools, steamwayland

FILE = "steam-undo.json"
UNDOABLE = ("set_runner", "set_wayland")
#: Undos kept per game, newest last. A second change used to replace the first one's undo, so
#: after the runner and the Wayland step the runner could no longer be taken back (#1031).
KEEP = 5


def _stack(data: dict[str, Any], app_id: str | int) -> list[dict[str, Any]]:
    """This game's saved undos, oldest first (a file from before the stack holds one dict)."""
    value = data.get(str(app_id))
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def _top(app_id: str | int, state_dir: Path | None) -> dict[str, Any] | None:
    stack = _stack(_read(state_dir), app_id)
    return stack[-1] if stack else None


def _path(state_dir: Path | None) -> Path:
    return (Path(state_dir) if state_dir is not None else paths.state_dir()) / FILE


def _read(state_dir: Path | None) -> dict[str, Any]:
    try:
        data = json.loads(_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any], state_dir: Path | None) -> None:
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".steam-undo.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _encode(action: str, payload: object) -> dict[str, Any] | None:
    if action == "set_runner":
        if payload is None:
            return {"kind": "none"}
        if isinstance(payload, steamtools.CompatEntry):
            return {
                "kind": "entry",
                "app_id": payload.app_id,
                "fields": dict(payload.fields),
                "text": payload.text,
            }
    if action == "set_wayland" and isinstance(payload, steamwayland.LaunchUndo):
        blob: dict[str, Any] = {"kind": "launch", "previous": payload.previous}
        if payload.written is not steamwayland.UNKNOWN:
            blob["written"] = payload.written
        return blob
    return None


def _decode(action: str, blob: object) -> tuple[bool, object]:
    """``(ok, payload)``; ``ok`` is False for anything that is not a valid saved undo."""
    if not isinstance(blob, dict):
        return False, None
    kind = blob.get("kind")
    if action == "set_runner" and kind == "none":
        return True, None
    if action == "set_runner" and kind == "entry":
        fields, text, app_id = blob.get("fields"), blob.get("text"), blob.get("app_id")
        if isinstance(fields, dict) and isinstance(text, str) and isinstance(app_id, str):
            clean = {str(k): str(v) for k, v in fields.items()}
            return True, steamtools.CompatEntry(app_id, clean, text)
    if action == "set_wayland" and kind == "launch":
        previous = blob.get("previous")
        written = blob.get("written", steamwayland.UNKNOWN)
        if (previous is None or isinstance(previous, str)) and (
            written is None or isinstance(written, str) or written is steamwayland.UNKNOWN
        ):
            return True, steamwayland.LaunchUndo(previous, written)
    return False, None


def save(
    app_id: str | int,
    action: str,
    payload: object,
    *,
    where: str = "",
    state_dir: Path | None = None,
) -> None:
    """Remember how to undo this game's change, on top of its earlier undos (``KEEP`` at most).

    ``where`` names the file the change was made in (the Steam account's ``localconfig.vdf``
    for launch options): an undo only fits the account it was made for (#894)."""
    encoded = _encode(action, payload) if action in UNDOABLE else None
    if encoded is None:
        return
    data = _read(state_dir)
    stack = _stack(data, app_id)
    stack.append({"action": action, "payload": encoded, "where": str(where or "")})
    data[str(app_id)] = stack[-KEEP:]
    _write(data, state_dir)


def where(app_id: str | int, *, state_dir: Path | None = None) -> str:
    """The file this game's newest saved undo was made in (empty for an older undo or none)."""
    entry = _top(app_id, state_dir)
    return str(entry.get("where") or "") if entry is not None else ""


def fits(app_id: str | int, current: str, *, state_dir: Path | None = None) -> bool:
    """Whether the saved undo may be applied to ``current`` (the file in use now).

    A launch-option undo is for one Steam account (its ``localconfig.vdf``), a runner undo for
    one Steam installation (its ``config.vdf``): native and Flatpak Steam keep their own, and
    restoring one into the other would overwrite what that install has (#894, #970). An undo
    saved without the file it was made in is not trusted.
    """
    if load(app_id, state_dir=state_dir) is None:
        return False
    recorded = where(app_id, state_dir=state_dir)
    return bool(recorded) and recorded == str(current)


def load(app_id: str | int, *, state_dir: Path | None = None) -> tuple[str, object] | None:
    """``(action, what the undo needs)`` of this game's newest undo, or ``None``."""
    entry = _top(app_id, state_dir)
    if entry is None or entry.get("action") not in UNDOABLE:
        return None
    ok, payload = _decode(str(entry["action"]), entry.get("payload"))
    return (str(entry["action"]), payload) if ok else None


def clear(app_id: str | int, *, state_dir: Path | None = None) -> None:
    """Drop this game's newest undo (it was used or no longer fits); the one before is next."""
    data = _read(state_dir)
    if str(app_id) not in data:
        return
    stack = _stack(data, app_id)[:-1]
    if stack:
        data[str(app_id)] = stack
    else:
        del data[str(app_id)]
    _write(data, state_dir)
