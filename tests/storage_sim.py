# SPDX-License-Identifier: GPL-3.0-or-later
"""A storage simulator: random worlds, random real actions, invariants after every step (#1372).

Run ``python tests/storage_sim.py --runs 1000`` for the big run; ``tests/test_storage_sim.py``
runs a fixed handful of seeds in CI. Every run is reproducible from its seed. A failing run is
shrunk to the shortest action list that still fails the same way.

Worlds come from ``tests/storage_shapes.py`` (a random mix of shapes) plus the player's own files
that Nadare must never touch. Actions drive the same core calls the GUI makes: Move a whole game
or only its prefix (same disk or another disk, optionally crashing at a step), Uninstall with a
random choice of boxes, Uninstall everything, Find games + adopt, and a restart (reload the config,
start-up upkeep, finishing pending originals). Before and after each action every file is listed
and these must hold:

* nothing is deleted that the action was not allowed to delete (never the player's files, never
  another game's folders);
* a finished Move leaves every file at the destination; a crash never loses a file;
* a restart deletes nothing but a verified pending original;
* the games still point at folders that exist (a crash may leave a known gap, labelled);
* no two games start sharing a folder;
* every saved catalog game has a current ``location`` record, and where that record decides it
  gives the same answer as the legacy keys (#1372 phase 2a/2b);
* a restart settles an interrupted import-move (its record does not survive a restart).

An import-move (Settings → "Move into a library") is an action too, crashing
mid-way or before its record.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import shutil
import sys
import tempfile
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent / "src") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "src"))

from storage_shapes import SHAPES, World  # noqa: E402

from nadare.core import (  # noqa: E402
    folderproof,
    importmove,
    libraries,
    location,
    paths,
    prefixguard,
    relink,
    relocate,
    removal,
    uninstall_all,
)
from nadare.core.config import Config  # noqa: E402

#: Shapes mixed into random worlds (flxpc_world is its own world: its names overlap others).
MIXABLE = sorted(name for name in SHAPES if name != "flxpc_world")
LIBRARIES = ("{LIB}", "{LIB2}", "{LIB3}")
SENTINELS = (
    "{HOME}/Documents/thesis.txt",
    "{HOME}/Games/Notes/readme.txt",
    "{LIB}/Other/keep.dat",
    "{LIB2}/Other/keep.dat",
)
CRASHES = ("none", "after_copy", "after_save")
#: An import moved into a library (#1372 2c): killed after its first folder, or after every
#: folder but before the record (the next start settles both, phase 3).
IMPORT_CRASHES = ("none", "none", "mid", "after_run")


@dataclass
class Violation:
    category: str
    detail: str


@dataclass
class Run:
    seed: int
    shapes: list[str]
    actions: list[tuple]
    violations: list[Violation] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    legacy_extra_profiles: int = 0


# ---- environment -------------------------------------------------------------------------
@contextlib.contextmanager
def _env(base: Path) -> Iterator[None]:
    detached = (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "SESSION_MANAGER",
        "APPIMAGE",
        "APPDIR",
        "FLATPAK_ID",
        "SNAP",
        "SNAP_USER_DATA",
    )
    keys = (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "NADARE_NO_KEYRING",
        *detached,
    )
    old = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = str(base / "home")
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        os.environ[f"XDG_{key}_HOME"] = str(base / key.lower())
    runtime = base / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)
    # An absent address can trigger D-Bus autolaunch; use a deliberately unavailable bus.
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(runtime / "no-session-bus")
    os.environ["NADARE_NO_KEYRING"] = "1"
    for key in detached:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _cross_disk(on: bool) -> Iterator[None]:
    real = relocate._same_device
    if on:
        relocate._same_device = lambda src, dst: False
    try:
        yield
    finally:
        relocate._same_device = real


# ---- the world -----------------------------------------------------------------------------
def build_world(seed: int, base: Path) -> tuple[Config, list[str], World]:
    rng = random.Random(seed)
    world = World(base)
    (base / "home").mkdir(parents=True, exist_ok=True)
    (base / "data" / "nadare" / "profiles").mkdir(parents=True, exist_ok=True)
    for lib in LIBRARIES:
        world.path(lib).mkdir(parents=True, exist_ok=True)
    if rng.random() < 0.1:
        chosen = ["flxpc_world"]
    else:
        chosen = rng.sample(MIXABLE, rng.randint(1, 5))
    data: dict[str, Any] = {"profiles": {}, "libraries": []}
    used: list[str] = []
    for shape in chosen:
        part = SHAPES[shape](world)
        if set(part.get("profiles", {})) & set(data["profiles"]):
            continue  # two shapes with one profile name: keep the first
        data["profiles"].update(part.get("profiles", {}))
        for entry in part.get("libraries", []) or []:
            if entry not in data["libraries"]:
                data["libraries"].append(entry)
        used.append(shape)
    for lib in ("{LIB}", "{LIB2}", "{LIB3}"):
        entry = {"path": world.fill(lib)}
        if entry not in data["libraries"] and rng.random() < 0.5:
            data["libraries"].append(entry)
    for sentinel in SENTINELS:
        path = world.path(sentinel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"player data {sentinel}")
    # Preserve the effective folders while varying their historical storage location.
    for profile in data["profiles"].values():
        if rng.random() < 0.25:
            extra = profile.setdefault("extra", {})
            for key in ("prefix", "install_dir"):
                value = profile.get(key)
                if isinstance(value, str) and value:
                    extra[key] = profile.pop(key)
    cfg_path = base / "config.json"
    cfg_path.write_text(json.dumps(data))
    return Config.load(cfg_path), used, world


def inventory(base: Path) -> dict[str, int]:
    """Every regular file the player or a game owns: ``{path: size}`` (links not followed)."""
    found: dict[str, int] = {}
    for top in ("home", "lib", "lib2", "lib3", "mnt", "data/nadare/profiles"):
        root = base / top
        if not root.exists():
            continue
        for current, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                full = Path(current, name)
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    found[str(full)] = full.stat().st_size
                except OSError:
                    continue
    return found


def _under(file: str, folder: Path) -> bool:
    try:
        return Path(file) == folder or folder in Path(file).parents
    except (OSError, ValueError):
        return False


def _real(text: Any) -> Path | None:
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        return None
    try:
        return Path(os.path.realpath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return None


def _trees(profile: dict[str, Any]) -> list[Path]:
    out = []
    for key in ("install_dir", "prefix", "wineprefix"):
        path = _real(location._setting(profile, key))
        if path is not None:
            out.append(path)
    return out


# ---- actions ---------------------------------------------------------------------------------
def _pick(cfg: Config, index: int) -> str | None:
    names = sorted(cfg.profiles)
    return names[index % len(names)] if names else None


def _record(cfg: Config, profile: dict, src: Path, dst: Path) -> None:
    """As the GUI does before a Move (#1366 B-F2)."""
    with cfg.locked():
        relocate.begin_move(profile, src, dst)
        cfg.save()


def _forget(cfg: Config, profile: dict) -> None:
    with cfg.locked():
        if relocate.end_move(profile):
            cfg.save()


def act_move_whole(ctx: dict, cfg: Config, a: tuple) -> dict:
    _, index, lib, cross, crash = a
    name = _pick(cfg, index)
    if name is None:
        return {"skipped": "no profiles"}
    profile = cfg.profiles[name]
    home = relink.managed_home(profile)
    if home is None:
        return {"skipped": "not movable whole"}
    library = ctx["world"].path(lib)
    try:
        relink.check_movable(profile, name=name, profiles=cfg.profiles)
        relink.check_home_target(profile, library, name=name, profiles=cfg.profiles)
    except relink.RelinkError as exc:
        return {"refused": str(exc)}
    dst = relink.free_home(library, relink.home_name(profile, home), cfg.profiles)
    install = location._setting(profile, "install_dir") or ""
    install_existed = bool(install) and Path(os.path.expanduser(install)).is_dir()
    _record(cfg, profile, home, dst)
    with _cross_disk(cross):
        relink.clear_stale_folder(dst, cfg.profiles)
        try:
            moved = relocate.move_tree(
                home, dst, journal=profile[relocate.MOVE_IN_PROGRESS], checkpoint=cfg.save
            )
        except relocate.RelocateError as exc:
            _forget(cfg, profile)
            return {"refused": str(exc)}
    out = {"src": home, "dst": dst, "kept": moved.kept_old, "crash": crash}
    if crash == "after_copy":
        return out
    plan = relink.plan_home_move(profile, dst, install_existed=install_existed)
    changes = dict(plan.changes)
    if moved.kept_old is not None:
        changes = relocate.with_pending_original(changes, profile, moved.kept_old, dst)
    with cfg.locked():
        profile.update(changes)
        libraries.sync_game(cfg, name, remember_library=True)
        cfg.save()
    _forget(cfg, profile)
    if crash == "after_save" or moved.kept_old is None:
        return out
    outcome = relocate.remove_original(moved.kept_old, dst)
    with cfg.locked():
        if relocate.forget_pending_original(profile, moved.kept_old):
            cfg.save()
    out["removed"] = outcome.removed
    return out


def act_move_prefix(ctx: dict, cfg: Config, a: tuple) -> dict:
    _, index, lib, cross, crash = a
    name = _pick(cfg, index)
    if name is None:
        return {"skipped": "no profiles"}
    profile = cfg.profiles[name]
    if relink.managed_home(profile) is not None:
        return {"skipped": "would be a whole-game move"}
    text = location._setting(profile, "prefix")
    if not isinstance(text, str) or not text.strip():
        return {"skipped": "no prefix"}
    old = Path(os.path.expanduser(text))
    if not old.is_dir():
        return {"skipped": "prefix missing"}
    try:
        relink.check_movable(profile, name=name, profiles=cfg.profiles)
    except relink.RelinkError as exc:
        return {"refused": str(exc)}
    parent = ctx["world"].path(lib) / "Prefixes"
    parent.mkdir(parents=True, exist_ok=True)
    dst = relink.prefix_destination(profile, old, parent)
    try:
        relink.check_place(dst)
    except relink.RelinkError as exc:
        return {"refused": str(exc)}
    clash = prefixguard.unsafe_reason(dst, profiles=cfg.profiles, own=name)
    if clash:
        return {"refused": clash}
    made = removal.prefix_made_by_nadare(profile, old, name)
    _record(cfg, profile, old, dst)
    with _cross_disk(cross):
        try:
            moved = relocate.move_tree(
                old, dst, journal=profile[relocate.MOVE_IN_PROGRESS], checkpoint=cfg.save
            )
        except relocate.RelocateError as exc:
            _forget(cfg, profile)
            return {"refused": str(exc)}
    out = {"src": old, "dst": dst, "kept": moved.kept_old, "crash": crash, "made": made}
    if crash == "after_copy":
        return out
    plan = relink.plan_relink(profile, dst, made_by_nadare=True)
    changes = dict(plan.changes)
    if moved.kept_old is not None and made:
        changes = relocate.with_pending_original(changes, profile, moved.kept_old, dst)
    with cfg.locked():
        profile.update(changes)
        libraries.sync_game(cfg, name, remember_library=False)
        cfg.save()
    _forget(cfg, profile)
    if crash == "after_save" or moved.kept_old is None or not made:
        return out
    outcome = relocate.remove_original(moved.kept_old, dst)
    with cfg.locked():
        if relocate.forget_pending_original(profile, moved.kept_old):
            cfg.save()
    out["removed"] = outcome.removed
    return out


def act_uninstall(ctx: dict, cfg: Config, a: tuple) -> dict:
    _, index, which, delete_data = a
    name = _pick(cfg, index)
    if name is None:
        return {"skipped": "no profiles"}
    safe = [t for t in removal.removable_trees(cfg, name) if t.safe]
    chosen = [t for i, t in enumerate(safe) if which & (1 << i)]
    result = removal.remove_game(
        cfg,
        name,
        delete_trees=[t.key for t in chosen],
        delete_data=delete_data,
        applications_dir=ctx["base"] / "apps",
        desktop_dir=ctx["base"] / "desktop",
    )
    return {
        "name": name,
        "trees": [t.path for t in chosen],
        "data": delete_data,
        "refused": bool(getattr(result, "refused", False)),
    }


def act_uninstall_all(ctx: dict, cfg: Config, a: tuple) -> dict:
    _, games = a
    safe = {n: [t.path for t in removal.removable_trees(cfg, n) if t.safe] for n in cfg.profiles}
    uninstall_all.run(
        cfg,
        games=games,
        applications_dir=ctx["base"] / "apps",
        desktop_dir=ctx["base"] / "desktop",
    )
    return {"games": games, "safe": safe}


def act_find_games(ctx: dict, cfg: Config, a: tuple) -> dict:
    adopted = []
    for root in [*libraries.library_roots(cfg), libraries.default_root()]:
        found, _notes = libraries.scan(cfg, root)
        for candidate in found[:1]:
            with cfg.locked():
                adopted.append(libraries.adopt(cfg, candidate))
                cfg.save()
    return {"adopted": adopted}


def act_import_move(ctx: dict, cfg: Config, a: tuple) -> dict:
    """Move an import into a library, through Settings for an existing imported profile."""
    _, index, lib, cross, crash = a
    name = _pick(cfg, index)
    if name is None:
        return {"skipped": "no profiles"}
    if not importmove.movable(cfg, name):
        return {"skipped": "not a movable import"}
    try:
        move = importmove.plan(cfg, name, ctx["world"].path(lib))
    except relink.RelinkError as exc:
        return {"refused": str(exc)}
    out: dict[str, Any] = {
        # Real paths: a folder reached through a link the player made moves its real files.
        "moves": [(os.path.realpath(src), str(dst)) for _key, src, dst in move.trees],
        "crash": crash,
    }
    with _cross_disk(cross):
        if crash == "mid":
            # The real run, killed when its second folder starts (after the first one).
            try:
                _killed_run(move)
            except relocate.RelocateError as exc:
                out["refused"] = str(exc)
            return out
        try:
            importmove.run(move)
        except relocate.RelocateError as exc:
            out["refused"] = str(exc)
            return out
    if crash == "after_run":
        return out
    with cfg.locked():
        importmove.commit(cfg, name, move)
        cfg.save()
    return out


class _Killed(BaseException):
    """The process died: no rollback, no clean-up runs."""


def _killed_run(move: importmove.ImportMove) -> None:
    real_move = relocate.move_tree
    real_undo, real_clear = importmove._undo, importmove._clear_journal
    calls: list[Path] = []

    def dying(src, dst, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise _Killed
        return real_move(src, dst, **kwargs)

    relocate.move_tree = dying
    importmove._undo = lambda *a: None
    importmove._clear_journal = lambda m: None
    try:
        importmove.run(move)
    except _Killed:
        pass
    finally:
        relocate.move_tree = real_move
        importmove._undo = real_undo
        importmove._clear_journal = real_clear


def _import_journal(cfg: Config) -> dict | None:
    return importmove._read_journal(cfg)  # noqa: SLF001 - what the next start will read


def act_restart(ctx: dict, cfg: Config, a: tuple) -> dict:
    fresh = Config.load(cfg.path)
    journal = _import_journal(fresh)
    put_back = []
    if journal is not None and isinstance(journal.get("trees"), list):
        put_back = [
            (t.get("destination"), t.get("source"))
            for t in journal["trees"]
            if isinstance(t, dict) and t.get("destination") and t.get("source")
        ]
    with fresh.locked():
        if libraries.backfill(fresh):
            fresh.save()
    finished = []
    for name, old, new in relocate.pending_originals(fresh):
        outcome = relocate.remove_original(old, new)
        with fresh.locked():
            if relocate.forget_pending_original(fresh.profiles[name], old):
                fresh.save()
        finished.append((old, new, outcome.removed))
    ctx["cfg"] = fresh
    return {"finished": finished, "put_back": put_back}


ACTIONS = {
    "move_whole": act_move_whole,
    "move_prefix": act_move_prefix,
    "uninstall": act_uninstall,
    "uninstall_all": act_uninstall_all,
    "find_games": act_find_games,
    "restart": act_restart,
    "import_move": act_import_move,
}


def random_actions(rng: random.Random) -> list[tuple]:
    out: list[tuple] = []
    for _ in range(rng.randint(4, 10)):
        kind = rng.choices(list(ACTIONS), weights=[5, 4, 4, 1, 2, 3, 3], k=1)[0]
        if kind == "import_move":
            out.append(
                (
                    kind,
                    rng.randrange(8),
                    rng.choice(LIBRARIES),
                    rng.random() < 0.5,
                    rng.choice(IMPORT_CRASHES),
                )
            )
        elif kind in ("move_whole", "move_prefix"):
            out.append(
                (
                    kind,
                    rng.randrange(8),
                    rng.choice(LIBRARIES),
                    rng.random() < 0.6,
                    rng.choice(CRASHES),
                )
            )
        elif kind == "uninstall":
            out.append((kind, rng.randrange(8), rng.randrange(4), rng.random() < 0.5))
        elif kind == "uninstall_all":
            out.append((kind, rng.random() < 0.5))
        else:
            out.append((kind,))
    return out


# ---- invariants ----------------------------------------------------------------------------
def _check(
    ctx: dict,
    before: dict,
    after: dict,
    cfg_before: dict,
    action: tuple,
    result: dict,
    start_overlaps: set,
) -> list[Violation]:
    world: World = ctx["world"]
    v: list[Violation] = []
    missing = sorted(set(before) - set(after))
    sentinels = {str(world.path(s)) for s in SENTINELS}
    kind = action[0]
    for f in missing:
        if f in sentinels:
            v.append(Violation("deleted player file", f"{kind}: {f}"))
    others_trees: dict[str, list[Path]] = {
        n: _trees(p) for n, p in cfg_before.items() if isinstance(p, dict)
    }

    def arrived(f: str, src: Path, dst: Path) -> bool:
        rel = Path(f).relative_to(src)
        target = dst / rel
        return str(target) in after and after[str(target)] == before[f]

    if kind in ("move_whole", "move_prefix") and "src" in result:
        src, dst = Path(result["src"]), Path(result["dst"])
        for f in missing:
            if f in sentinels:
                continue
            if not _under(f, src):
                v.append(Violation("move deleted outside its folder", f"{kind}: {f}"))
            elif not arrived(f, src, dst):
                v.append(Violation("move lost a file", f"{kind} crash={result.get('crash')}: {f}"))
        if result.get("removed") and kind == "move_prefix" and not result.get("made"):
            v.append(Violation("move deleted a player's original", str(src)))
    elif kind == "import_move" and "moves" in result:
        pairs = [(Path(s), Path(d)) for s, d in result["moves"]]
        for f in missing:
            if f in sentinels:
                continue
            home = [(s, d) for s, d in pairs if _under(f, s)]
            if not home:
                v.append(Violation("import-move deleted outside its folders", f))
            elif not any(arrived(f, s, d) for s, d in home):
                v.append(Violation("import-move lost a file", f"crash={result['crash']}: {f}"))
    elif kind == "uninstall":
        name = result.get("name")
        allowed = [Path(p) for p in result.get("trees", [])]
        own = paths.profiles_dir() / name if name else None
        for f in missing:
            if f in sentinels:
                continue
            ok = any(_under(f, t) for t in allowed)
            if not ok and result.get("data") and own is not None and _under(f, own):
                ok = Path(os.path.realpath(f)) == Path(f)  # never through a link (B1)
            if not ok and Path(f).name in (
                libraries.MANIFEST_NAME, libraries.MANIFEST_TMP_NAME, *folderproof.MARKER_NAMES
            ):
                ok = True  # Nadare's own manifest (or proof marker) in the game's folder
            for other, trees in others_trees.items():
                if other != name and any(_under(f, t) for t in trees):
                    v.append(Violation("uninstall deleted another game's file", f"{name}: {f}"))
                    ok = True
            if not ok:
                v.append(Violation("uninstall deleted an unexpected file", f"{name}: {f}"))
    elif kind == "uninstall_all":
        safe = [Path(p) for paths_ in result["safe"].values() for p in paths_]
        data_dir = paths.data_dir()
        game_trees = [t for trees in others_trees.values() for t in trees]
        for f in missing:
            if f in sentinels or Path(f).name in (
                libraries.MANIFEST_NAME,
                libraries.MANIFEST_TMP_NAME,
                *folderproof.MARKER_NAMES,
            ):
                continue  # Nadare's own manifest (and proof markers) go with Nadare
            in_game_tree = any(_under(f, t) for t in game_trees)
            if in_game_tree and not (result["games"] and any(_under(f, t) for t in safe)):
                v.append(Violation("uninstall-all deleted a kept game file", f))
            elif not in_game_tree and not _under(f, data_dir):
                v.append(Violation("uninstall-all deleted outside Nadare", f))
    elif kind == "restart":
        for f in missing:
            if f in sentinels:
                continue
            ok = any(
                _under(f, Path(old)) and removed and (
                    arrived(f, Path(old), Path(new))
                    # Nadare's proof marker in the original the finished Move removed.
                    or Path(f).name in folderproof.MARKER_NAMES
                )
                for old, new, removed in result.get("finished", [])
            ) or any(
                # An interrupted import-move put a renamed folder back (phase 3).
                _under(f, Path(dst)) and arrived(f, Path(dst), Path(src))
                for dst, src in result.get("put_back", [])
            )
            if not ok:
                v.append(Violation("restart deleted a file", f))
    else:
        for f in missing:
            if f not in sentinels:
                v.append(Violation(f"{kind} deleted a file", f))

    # Games still point at folders that exist.
    cfg = ctx["cfg"]
    for name, profile in cfg.profiles.items():
        old = cfg_before.get(name) if isinstance(cfg_before.get(name), dict) else {}
        for key in ("install_dir", "prefix"):
            was = _real(location._setting(old, key))
            now = _real(location._setting(profile, key))
            if was is None or now is None or not str(before_dirs(before, was)):
                continue
            if any(_under(f, was) for f in before) and not now.exists():
                if relocate.MOVE_IN_PROGRESS in profile:
                    continue  # a crashed Move: the next start finishes it (checked then)
                journal = _import_journal(cfg)
                if journal is not None and journal.get("profile") == name:
                    continue  # a crashed import-move: the next start settles it (checked then)
                v.append(
                    Violation("game points at a missing folder", f"{kind}: {name}.{key} -> {now}")
                )
    # No new overlap between two games.
    for pair in _overlaps(cfg) - start_overlaps:
        v.append(Violation("two games share a folder", f"{kind}: {pair}"))
    v.extend(_record_violations(cfg, kind))
    return v


def _record_violations(cfg: Config, kind: str) -> list[Violation]:
    """The saved ``location`` record is current and agrees with the legacy keys (2a/2b)."""
    v: list[Violation] = []
    try:
        saved = json.loads(Path(cfg.path).read_text(encoding="utf-8")).get("profiles", {})
    except (OSError, ValueError, AttributeError):
        return [Violation("config unreadable", kind)]
    for name, profile in saved.items():
        if not isinstance(profile, dict) or not location.catalog_kind(profile.get("backend")):
            continue
        if location.RECORD_KEY not in profile:
            continue  # never saved since the world was written (a pre-2a config)
        if not location.record_is_current(profile):
            v.append(Violation("saved location record is stale", f"{kind}: {name}"))
            continue
        recorded = location.game_folder(profile, name)
        legacy = location._legacy_game_folder(profile, name)  # noqa: SLF001
        if recorded != legacy:
            v.append(
                Violation(
                    "location record disagrees with the legacy answer",
                    f"{kind}: {name}: record {recorded} legacy {legacy}",
                )
            )
    return v


def before_dirs(before: dict, folder: Path) -> bool:
    return any(_under(f, folder) for f in before)


def _overlaps(cfg: Config) -> set[tuple[str, str]]:
    found = set()
    for name, profile in cfg.profiles.items():
        if not isinstance(profile, dict):
            continue
        for tree in _trees(profile):
            claim = location.overlapping(
                {k: v for k, v in cfg.profiles.items() if k != name},
                tree,
                excluding=None,
                direction="either",
            )
            if claim is not None and claim.source == "path":
                found.add(tuple(sorted((name, claim.owner))))
    return found


# ---- running --------------------------------------------------------------------------------
def execute(seed: int, actions: list[tuple] | None = None, *, keep: Path | None = None) -> Run:
    rng = random.Random(seed)
    base = Path(tempfile.mkdtemp(prefix=f"nadare-sim-{seed}-")) if keep is None else keep
    try:
        with _env(base):
            cfg, shapes, world = build_world(seed, base)
            if actions is None:
                actions = random_actions(rng)
            run = Run(seed, shapes, list(actions))
            run.legacy_extra_profiles = sum(
                any(location._setting(p, k) and not p.get(k) for k in ("prefix", "install_dir"))
                for p in cfg.profiles.values()
            )
            ctx = {"cfg": cfg, "world": world, "base": base}
            start = _overlaps(cfg)
            for action in actions:
                if not _step(ctx, base, run, start, action):
                    break
            else:
                _final_restart(ctx, base, run, start)
        return run
    finally:
        if keep is None:
            shutil.rmtree(base, ignore_errors=True)


def _step(ctx: dict, base: Path, run: Run, start: set, action: tuple) -> bool:
    """Run one action and check it; False when the run stops (a violation or an exception)."""
    cfg = ctx["cfg"]
    before = inventory(base)
    snapshot = json.loads(json.dumps(cfg.profiles))
    try:
        result = ACTIONS[action[0]](ctx, cfg, action)
    except Exception:  # noqa: BLE001 - a crash in Nadare's code is a finding too
        run.violations.append(Violation("exception", f"{action}: {traceback.format_exc(limit=3)}"))
        return False
    run.log.append(f"{action} -> { {k: str(v) for k, v in result.items()} }")
    after = inventory(base)
    found = _check(ctx, before, after, snapshot, action, result, start)
    if found:
        run.violations.extend(found)
        return False
    crashed = result.get("crash") not in (None, "none") and "refused" not in result
    if crashed and ("moves" in result or "src" in result):
        # The process died: the next thing that happens is a start of Nadare.
        return _step(ctx, base, run, start, ("restart",))
    return True


def _final_restart(ctx: dict, base: Path, run: Run, start: set) -> None:
    """Every run ends with a start of Nadare, so a crash is always followed by its recovery;
    the games must then point at folders that exist."""
    cfg = ctx["cfg"]
    before = inventory(base)
    snapshot = json.loads(json.dumps(cfg.profiles))
    result = act_restart(ctx, cfg, ("restart",))
    after = inventory(base)
    run.violations.extend(_check(ctx, before, after, snapshot, ("restart",), result, start))
    for name, profile in ctx["cfg"].profiles.items():
        if relocate.MOVE_IN_PROGRESS in profile:
            run.violations.append(Violation("a Move record survived a restart", name))
    journal = _import_journal(ctx["cfg"])
    if journal is not None:
        run.violations.append(
            Violation("an import-move record survived a restart", str(journal.get("profile")))
        )
    for name, profile in ctx["cfg"].profiles.items():
        for key in ("install_dir", "prefix"):
            now = _real(location._setting(profile, key))
            if now is None or now.exists():
                continue
            old = snapshot.get(name) if isinstance(snapshot.get(name), dict) else {}
            if any(_under(f, now) for f in before) or _real(location._setting(old, key)) != now:
                run.violations.append(
                    Violation("after a restart a game points at a missing folder", f"{name}.{key}")
                )


def shrink(run: Run) -> Run:
    """The shortest action list that still fails with the same first category."""
    want = run.violations[0].category
    actions = list(run.actions)
    changed = True
    while changed:
        changed = False
        for i in range(len(actions)):
            trial = actions[:i] + actions[i + 1 :]
            if not trial:
                continue
            again = execute(run.seed, trial)
            if again.violations and again.violations[0].category == want:
                actions = trial
                changed = True
                break
    return execute(run.seed, actions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", type=Path, default=None, help="write the findings here")
    args = parser.parse_args(argv)
    failures: list[Run] = []
    by_category: dict[str, int] = {}
    for seed in range(args.seed, args.seed + args.runs):
        run = execute(seed)
        if run.violations:
            cat = run.violations[0].category
            by_category[cat] = by_category.get(cat, 0) + 1
            failures.append(run)
    print(f"{args.runs} runs, {len(failures)} failed")
    for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4}  {cat}")
    report = []
    seen: set[str] = set()
    for run in failures:
        cat = run.violations[0].category
        if cat in seen:
            continue  # one shrunk example per category
        seen.add(cat)
        small = shrink(run)
        report.append(
            {
                "category": cat,
                "seed": run.seed,
                "shapes": small.shapes,
                "actions": [list(a) for a in small.actions],
                "violation": small.violations[0].detail
                if small.violations
                else run.violations[0].detail,
                "log": small.log,
            }
        )
        print(f"\n== {cat} (seed {run.seed}, shapes {small.shapes})")
        for a in small.actions:
            print(f"   {a}")
        print(f"   -> {report[-1]['violation'][:300]}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
