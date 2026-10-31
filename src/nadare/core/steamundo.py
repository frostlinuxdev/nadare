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
        return {"kind": "launch", "previous": payload.previous}
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
        if previous is None or isinstance(previous, str):
            return True, steamwayland.LaunchUndo(previous)
    return False, None


def save(
    app_id: str | int, action: str, payload: object, *, state_dir: Path | None = None
) -> None:
    """Remember how to undo this game's last change (replacing any earlier one)."""
    encoded = _encode(action, payload) if action in UNDOABLE else None
    if encoded is None:
        return
    data = _read(state_dir)
    data[str(app_id)] = {"action": action, "payload": encoded}
    _write(data, state_dir)


def load(app_id: str | int, *, state_dir: Path | None = None) -> tuple[str, object] | None:
    """``(action, what the undo needs)`` for this game, or ``None``."""
    entry = _read(state_dir).get(str(app_id))
    if not isinstance(entry, dict) or entry.get("action") not in UNDOABLE:
        return None
    ok, payload = _decode(str(entry["action"]), entry.get("payload"))
    return (str(entry["action"]), payload) if ok else None


def clear(app_id: str | int, *, state_dir: Path | None = None) -> None:
    data = _read(state_dir)
    if str(app_id) in data:
        del data[str(app_id)]
        _write(data, state_dir)
