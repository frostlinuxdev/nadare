# SPDX-License-Identifier: GPL-3.0-or-later
"""What Nadare answers today about every game in every real-world storage shape (#1372 phase 0).

For each game the snapshot records every function that answers one of the storage questions
("which folder is this game's own", "may Uninstall delete this", "may Move take the whole
folder", "which library is it in", "is this folder used by another game", "what is missing"),
side by side, so two functions that disagree are visible in one place and any change shows.

The snapshot is ``tests/data/storage_shapes_golden.json``. A change to it must be deliberate:
regenerate with ``NADARE_UPDATE_GOLDEN=1 pytest tests/test_storage_shapes.py`` and review the
diff in the PR.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from storage_shapes import SHAPES, World

from nadare.core import gamefinder, gamefolders, libraries, prefixguard, relink, removal
from nadare.core.config import Config

GOLDEN = Path(__file__).parent / "data" / "storage_shapes_golden.json"
PROBE = "a-new-game"  # a profile that does not exist yet, asking "is this folder free?"


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a raise is an answer too
        return f"RAISES {type(exc).__name__}"


class _Text:
    """Turns answers into stable JSON: paths become placeholders, objects become dicts."""

    def __init__(self, base: Path):
        self.subs = []
        for real in {base, base.resolve()}:
            self.subs += [
                (str(real / "data" / "nadare"), "{DATA}"),
                (str(real / "home"), "{HOME}"),
                (str(real / "lib2"), "{LIB2}"),
                (str(real / "lib"), "{LIB}"),
                (str(real / "mnt"), "{MNT}"),
                (str(real), "{BASE}"),
            ]
        self.subs.sort(key=lambda pair: -len(pair[0]))

    def __call__(self, value: Any) -> Any:
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            for real, placeholder in self.subs:
                value = value.replace(real, placeholder)
            return value
        if isinstance(value, dict):
            return {str(self(k)): self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [self(v) for v in value]
            return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
        if hasattr(value, "__dataclass_fields__"):
            return {k: self(getattr(value, k)) for k in value.__dataclass_fields__}
        return value


def _answers(cfg: Config, name: str, text: _Text) -> dict[str, Any]:
    profile = cfg.profiles[name]
    game_folder = _call(libraries._game_folder, profile, name, None)
    out: dict[str, Any] = {
        # Q1: which folder is this game's own?
        "q1_libraries_game_folder": game_folder,
        "q1_gamefolders_placement": _call(gamefolders.placement, cfg, name),
        "q1_relink_managed_home": _call(relink.managed_home, profile),
        "q1_removal_wrapper_folders": _call(
            removal._wrapper_folders, profile, name, others=cfg.profiles
        ),
        "q1_gamefinder_recorded_game": _call(gamefinder._recorded_game, profile),
        # Q2: may Uninstall delete this tree? (Settings Remove; the backend purge rule below)
        "q2_removal_removable_trees": _call(
            lambda: [
                {"key": t.key, "path": t.path, "safe": t.safe, "reason": t.reason}
                for t in removal.removable_trees(cfg, name)
            ]
        ),
        # Q5: which library?
        "q5_library_of_game_dir": (
            _call(libraries.library_of_game_dir, game_folder)
            if isinstance(game_folder, Path)
            else None
        ),
        "q5_library_of_folder": (
            _call(gamefolders.library_of_folder, cfg, game_folder)
            if isinstance(game_folder, Path)
            else None
        ),
        # Q8: what is missing?
        "q8_folders_missing": _call(gamefinder.folders_missing, profile),
        "q8_has_game_folders": _call(libraries._has_game_folders, profile),
    }
    # Q4 (used by another game), for each folder this game records. Q2 has one rule since
    # phase 1 step 3 (#1372): Settings Remove; the dead backend purge answers were dropped.
    for key in ("prefix", "install_dir"):
        value = profile.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        target = Path(value)
        resolved = _call(Path.resolve, target)
        if isinstance(resolved, Path) and resolved != target:
            # Where a linked folder really is, asked from another game's side (review of #1376).
            out[f"q4_{key}_real"] = {
                "prefixguard_unsafe_reason": _call(
                    prefixguard.unsafe_reason, resolved, profiles=cfg.profiles, own=PROBE
                ),
                "relink_inside_other": _call(relink._inside_other, cfg.profiles, PROBE, resolved),
            }
        out[f"q4_{key}"] = {
            "removal_shared_with": _call(removal._shared_with, cfg.profiles, PROBE, resolved),
            "libraries_used_folders": _call(
                lambda r=resolved: r in libraries._used_folders(cfg.profiles)
            ),
            "gamefinder_used_by_other": _call(
                gamefinder._used_by_other, cfg.profiles, PROBE, target
            ),
            "relink_inside_other": _call(relink._inside_other, cfg.profiles, PROBE, target),
            "prefixguard_unsafe_reason": _call(
                prefixguard.unsafe_reason, target, profiles=cfg.profiles, own=PROBE
            ),
        }
    return text(out)


def _world(shape: str, base: Path, monkeypatch) -> Config:
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(base / key.lower()))
    monkeypatch.setenv("HOME", str(base / "home"))
    (base / "home").mkdir(parents=True, exist_ok=True)
    (base / "data" / "nadare" / "profiles").mkdir(parents=True, exist_ok=True)
    data = SHAPES[shape](World(base))
    path = base / "config.json"
    path.write_text(json.dumps(data))
    return Config.load(path)


def _snapshot(shape: str, base: Path, monkeypatch) -> dict[str, Any]:
    cfg = _world(shape, base, monkeypatch)
    text = _Text(base)
    return {name: _answers(cfg, name, text) for name in sorted(cfg.profiles)}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_todays_answers_are_unchanged(shape, tmp_path, monkeypatch):
    got = _snapshot(shape, tmp_path / "w", monkeypatch)
    if os.environ.get("NADARE_UPDATE_GOLDEN") == "1":
        golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        golden[shape] = got
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
        return
    golden = json.loads(GOLDEN.read_text())
    assert shape in golden, "new shape: regenerate the golden file (see the module text)"
    assert got == golden[shape]


def test_every_shape_has_a_golden_entry():
    golden = json.loads(GOLDEN.read_text())
    assert sorted(golden) == sorted(SHAPES)
