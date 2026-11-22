# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A read-only check for signs of a known incident in a game's own folders (#806).

The indicators are data, not code: a signed ``iocs.json`` from the index says which file names,
literal text or sizes to look for. There is no built-in list. Without a verified list the check
answers "No indicator list yet" and looks at nothing.

Everything here is bounded and read-only. Only the profile's prefix and install folder are
visited, links are never followed, a file is read up to a cap, and a total cap and cancellation
stop a long run. Matches keep only an indicator id and a count: no file content, no secret.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import atomicfile, hosting, indexfetch, paths, releasesig
from .config import profile_value, valid_profile_name

log = logging.getLogger(__name__)

#: A document without this version is not ours.
SCHEMA_VERSION = 1
#: More indicators than this are ignored (the rest of the document is still used).
MAX_INDICATORS = 200
#: Most bytes read from one file unless the indicator asks for less.
DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024
#: Most bytes read during one profile check.
TOTAL_READ_CAP = 64 * 1024 * 1024
#: Most files and folders visited for one profile.
MAX_FILES = 20_000
#: How old the last verified list may be before it is not used.
MAX_LIST_AGE_SECONDS = indexfetch.MAX_CACHE_AGE_SECONDS
#: A saved copy dated a little ahead of the clock is still usable.
_CLOCK_SKEW_SECONDS = 3600

LEVELS = ("exposed", "compromised")
#: The three answers, plus the two states that are not a verdict.
CLEAN = "clean"
INCOMPLETE = "incomplete"
EXPOSED = "exposed"
COMPROMISED = "compromised"
NO_LIST = "no_list"
CANCELLED = "cancelled"
#: The check itself failed (a bug or an unreadable state folder); nothing was learned.
ERROR = "error"

_INDICATOR_ID_MAX = 100
_INDICATOR_TITLE_MAX = 200
_MAX_BODY = 65536
_MAX_SIGNATURE = 16384


@dataclass(frozen=True)
class Indicator:
    """One pure data rule from the signed list."""

    id: str
    title: str
    kinds: tuple[str, ...]
    level: str
    rule: dict[str, Any]

    def applies_to(self, kind: str) -> bool:
        return "*" in self.kinds or kind in self.kinds


@dataclass(frozen=True)
class Match:
    """An indicator that matched, and in how many entries."""

    id: str
    count: int
    level: str


@dataclass(frozen=True)
class SafetyResult:
    """What one profile check found. Never holds file content."""

    level: str
    matches: tuple[Match, ...] = ()
    checked_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    files_visited: int = 0
    bytes_read: int = 0
    truncated: bool = False
    cancelled: bool = False
    dismissed: bool = False
    compromise_warning: bool = False


# ---- the signed list -------------------------------------------------------------------------
def indicators_cache_path() -> Path:
    """The last verified body/signature pair, kept out of the build and the config."""
    return paths.cache_dir() / "safety" / "iocs.json"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def parse_indicators(doc: Any) -> tuple[list[Indicator], int]:
    """Valid indicators from a version 1 document, and how many rows were skipped."""
    indicators, skipped, _serial = _parse_document(doc)
    return indicators, skipped


def _parse_document(doc: Any) -> tuple[list[Indicator], int, int]:
    """The same document plus its anti-rollback serial."""
    if (
        not isinstance(doc, dict)
        or type(doc.get("version")) is not int
        or doc["version"] != SCHEMA_VERSION
        or type(doc.get("serial")) is not int
        or doc["serial"] < 1
        or not isinstance(doc.get("indicators"), list)
    ):
        raise ValueError("not a version 1 indicator document")
    parsed: list[Indicator] = []
    seen: set[str] = set()
    skipped = 0
    for row in doc["indicators"][:MAX_INDICATORS]:
        indicator = _parse_indicator(row, seen)
        if indicator is None:
            skipped += 1
            continue
        parsed.append(indicator)
        seen.add(indicator.id)
    skipped += max(0, len(doc["indicators"]) - MAX_INDICATORS)
    return parsed, skipped, doc["serial"]


def _parse_indicator(row: Any, seen: set[str]) -> Indicator | None:
    if not isinstance(row, dict):
        return None
    identifier = row.get("id")
    title = row.get("title")
    level = row.get("level")
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier) > _INDICATOR_ID_MAX
        or identifier in seen
        or not isinstance(title, str)
        or not title
        or len(title) > _INDICATOR_TITLE_MAX
        or level not in LEVELS
    ):
        return None
    kinds = row.get("kinds", ["*"])
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(not isinstance(kind, str) or not kind or len(kind) > 100 for kind in kinds)
    ):
        return None
    rule = _parse_rule(row.get("rule"))
    if rule is None:
        return None
    return Indicator(identifier, title, tuple(dict.fromkeys(kinds)), level, rule)


def _parse_glob(value: Any) -> str | None:
    """A relative glob that cannot leave the profile's folders."""
    if not isinstance(value, str) or not value or len(value) > 500:
        return None
    if value.startswith(("/", "\\", "~")) or "\x00" in value or "\\" in value:
        return None
    parts = value.split("/")
    if any(not part or part in (".", "..") for part in parts):
        return None
    return value


def _parse_rule(rule: Any) -> dict[str, Any] | None:
    if not isinstance(rule, dict):
        return None
    kind = rule.get("type")
    if kind == "path_exists":
        glob = _parse_glob(rule.get("glob"))
        return {"type": kind, "glob": glob} if glob else None
    if kind == "file_contains":
        glob = _parse_glob(rule.get("glob"))
        text = rule.get("text")
        if not glob or not isinstance(text, str) or not text or len(text) > 4096:
            return None
        limit = rule.get("max_bytes", DEFAULT_MAX_FILE_BYTES)
        if type(limit) is not int or limit <= 0:
            return None
        return {"type": kind, "glob": glob, "text": text, "max_bytes": limit}
    if kind == "file_size":
        glob = _parse_glob(rule.get("glob"))
        minimum = rule.get("min")
        maximum = rule.get("max")
        if not glob or type(minimum) is not int or type(maximum) is not int:
            return None
        if minimum < 0 or maximum < minimum:
            return None
        return {"type": kind, "glob": glob, "min": minimum, "max": maximum}
    return None


def verified_indicators(envelope: Any) -> list[Indicator]:
    """Indicators from a ``{"body", "signature"}`` pair signed by the release key; else raise."""
    return _verified_list(envelope)[0]


def _verified_list(envelope: Any) -> tuple[list[Indicator], int, str]:
    """Indicators, serial and the body's sha256 from a signed pair; else raise."""
    if not isinstance(envelope, dict):
        raise ValueError("missing signed body")
    body, signature = envelope.get("body"), envelope.get("signature")
    if not isinstance(body, str) or len(body.encode()) > _MAX_BODY:
        raise ValueError("invalid body")
    if not isinstance(signature, str) or len(signature) > _MAX_SIGNATURE:
        raise ValueError("invalid signature")
    raw = body.encode("utf-8")
    # Verify the exact bytes before parsing or trusting any field.
    with tempfile.TemporaryDirectory(prefix="nadare-safety-signature-") as folder:
        target = Path(folder) / "iocs.json"
        target.write_bytes(raw)
        releasesig.verify_detached(target, signature)
    doc = json.loads(body, object_pairs_hook=_unique)
    indicators, _skipped, serial = _parse_document(doc)
    return indicators, serial, hashlib.sha256(raw).hexdigest()


def _usable_age(age: float) -> bool:
    return -_CLOCK_SKEW_SECONDS <= age <= MAX_LIST_AGE_SECONDS


def _write_cache(path: Path, envelope: dict) -> None:
    try:
        atomicfile.write(path, json.dumps(envelope), make_parent=True, sync_dir=True)
    except OSError as exc:
        log.debug("cannot cache the safety indicator list: %s", exc)


def iocs_serial_path() -> Path:
    """The highest accepted list serial and the sha256 of its body (anti-rollback)."""
    return paths.state_dir() / "safety" / "iocs-serial.json"


def _read_serial(path: Path) -> dict | None:
    """The saved watermark; ``None`` when it is missing or unreadable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Ignoring the safety list watermark: %s", exc)
        return None
    try:
        if len(raw) > 4096:
            raise ValueError("watermark is too large")
        data = json.loads(raw)
        serial, digest = data["serial"], data["sha256"]
        if (
            type(serial) is not int
            or serial < 1
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ValueError("invalid watermark")
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("Ignoring an unreadable safety list watermark: %s", exc)
        return None
    return {"serial": serial, "sha256": digest}


def _serial_accepted(watermark: dict | None, serial: int, digest: str) -> bool:
    """A list is fresh unless it is older than, or differs at, the accepted serial."""
    if watermark is None or serial > watermark["serial"]:
        return True
    if serial < watermark["serial"]:
        return False
    return digest == watermark["sha256"]


def _write_serial(path: Path, serial: int, digest: str) -> None:
    atomicfile.write(
        path,
        json.dumps({"serial": serial, "sha256": digest}),
        make_parent=True,
        sync_dir=True,
    )


@contextmanager
def _list_lock(path: Path):
    """Serialize a read/modify/atomic-write transaction across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_indicators(
    *,
    opener=None,
    cache: str | os.PathLike[str] | None = None,
) -> list[Indicator] | None:
    """The verified list from a mirror, else the last verified copy; ``None`` when neither.

    An unverified or malformed document is never used and never replaces the saved copy. A
    list older than the last accepted serial, or another body at the same serial, is rejected
    as a replay, like a bad signature.
    """
    target = Path(cache) if cache is not None else indicators_cache_path()
    watermark_path = iocs_serial_path()
    fetch = opener or indexfetch.open_bytes
    for base in hosting.index_bases():
        if not base.startswith("https://"):
            continue
        try:
            body = fetch(base + "iocs.json")
            signature = fetch(base + "iocs.json.asc")
            envelope = {"body": body.decode("utf-8"), "signature": signature.decode("ascii")}
            indicators, serial, digest = _verified_list(envelope)
            with _list_lock(watermark_path):
                if not _serial_accepted(_read_serial(watermark_path), serial, digest):
                    raise ValueError("replayed indicator list")
                # Persist protection before publishing/returning the accepted body.
                _write_serial(watermark_path, serial, digest)
                _write_cache(target, envelope)
            return indicators
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            log.debug("safety list rejected from %s: %s", base, exc)
            continue

    try:
        with _list_lock(watermark_path):
            if not _usable_age(time.time() - target.stat().st_mtime):
                return None
            envelope = json.loads(target.read_text())
            indicators, serial, digest = _verified_list(envelope)
            if not _serial_accepted(_read_serial(watermark_path), serial, digest):
                raise ValueError("replayed saved list")
            _write_serial(watermark_path, serial, digest)
            return indicators
    except Exception:  # noqa: BLE001 - no protected, verified saved copy
        return None


# ---- the scan --------------------------------------------------------------------------------
#: Deepest folder chain opened below a configured root. Deeper trees are crafted, not installs;
#: the scan stops there and marks its coverage incomplete.
MAX_DEPTH = 64


def _configured_path(raw: Any) -> Path | None:
    """A profile path as an absolute, lexically normalized path (links are not resolved)."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return Path(os.path.abspath(os.path.expanduser(text)))
    except (OSError, RuntimeError):
        return Path(text)


def _profile_prefix(profile: dict, name: str) -> Path | None:
    """The prefix the profile's backend would use, without touching the disk.

    A configured ``prefix``/``wineprefix`` wins. When neither is set, only a backend whose
    Wine prefix lives in the profile folder gets that default: no other backend invents one.
    """
    configured = profile_value(profile, "prefix") or profile_value(profile, "wineprefix")
    if configured not in (None, ""):
        return _configured_path(configured)
    kind = str(profile.get("backend") or "")
    if not kind or not name or not valid_profile_name(name):
        return None
    # Local import: the backend registry imports much of the app, and core must not need it
    # just to import this module.
    from .backends import get as backend_class
    from .backends import wine_prefix

    try:
        backend = backend_class(kind)
    except Exception:  # noqa: BLE001 - an unknown/broken kind simply has no default prefix
        return None
    if not getattr(backend, "uses_wine_prefix", False):
        return None
    return _configured_path(wine_prefix(profile, paths.profiles_dir() / name))


def _roots(profile: dict, name: str = "") -> list[Path]:
    prefix = _profile_prefix(profile, name)
    install = _configured_path(profile_value(profile, "install_dir"))
    candidates: list[Path] = []
    if prefix is not None:
        candidates.append(prefix)
    # A folder configured inside another one is reached through it, never as a second root:
    # a game can replace it with a link, and following that link is what picked a folder
    # outside the prefix before this check existed.
    if install is not None and not (
        prefix is not None and (install == prefix or prefix in install.parents)
    ):
        candidates.append(install)

    chosen: list[Path] = []
    for folder in candidates:
        try:
            resolved = folder.resolve()
        except (OSError, RuntimeError):
            # Keep failed roots visible to the scan so coverage cannot silently look complete.
            resolved = folder.absolute()
        if resolved in chosen or any(
            resolved == other or other in resolved.parents for other in chosen
        ):
            continue
        chosen = [other for other in chosen if not (other == resolved or resolved in other.parents)]
        chosen.append(resolved)
    return chosen


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class _Directory:
    """A folder below a held root, named by its path AND the identity of every folder on it.

    Only the root stays open: a prefix has thousands of folders and desktops often allow
    1024 open files. Each use re-walks from the root without following links and checks
    every folder is still the one the scan saw, so a folder swapped mid-check is refused.
    """

    root_fd: int
    parts: tuple[str, ...] = ()
    idents: tuple[tuple[int, int], ...] = ()

    def child(self, name: str, ident: tuple[int, int]) -> _Directory:
        return _Directory(self.root_fd, (*self.parts, name), (*self.idents, ident))

    def open(self) -> int:
        """A new descriptor for this folder; the caller closes it."""
        fd = os.dup(self.root_fd)
        try:
            for part, ident in zip(self.parts, self.idents, strict=True):
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != ident:
                    raise OSError(f"{part} changed during the check")
        except BaseException:
            os.close(fd)
            raise
        return fd


@dataclass
class _File:
    parent: _Directory
    name: str


def _open_root(root: Path, handles: ExitStack) -> _Directory:
    # Root selection may resolve a configured link once. Anchor every subsequent component.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in root.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    handles.callback(os.close, fd)
    return _Directory(fd)


def _entries(root: _Directory, cancel, limit: int):
    """Bounded enumeration; one descriptor per folder on the path being walked.

    Each folder is opened once, through its parent's descriptor, so a deep chain costs one
    ``open`` per folder instead of re-walking the whole ancestor chain for every entry. The
    walk stops at :data:`MAX_DEPTH` (a crafted tree) and reports incomplete coverage, and it
    re-checks cancellation between entries.
    """
    found: list[tuple[str, int, int, _File]] = []
    incomplete = False

    def walk(fd: int, folder: _Directory, prefix: str, depth: int) -> str | None:
        """Enumerate one folder; returns ``"cancel"``/``"limit"`` when it must stop."""
        nonlocal incomplete
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if cancel is not None and cancel.is_set():
                        return "cancel"
                    if len(found) >= limit:
                        return "limit"
                    rel = f"{prefix}/{entry.name}" if prefix else entry.name
                    try:
                        info = entry.stat(follow_symlinks=False)
                        found.append((rel, info.st_mode, info.st_size, _File(folder, entry.name)))
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        if depth >= MAX_DEPTH:
                            incomplete = True
                            continue
                        child = folder.child(entry.name, (info.st_dev, info.st_ino))
                        child_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                        try:
                            stopped = walk(child_fd, child, rel, depth + 1)
                            if stopped is not None:
                                return stopped
                        finally:
                            os.close(child_fd)
                    except OSError:
                        if len(found) == 0 or found[-1][0] != rel:
                            found.append((rel, 0, 0, _File(folder, entry.name)))
                        incomplete = True
        except OSError:
            incomplete = True
        return None

    # The caller holds the root descriptor for the whole check; enumerate from a duplicate
    # so closing this walk cannot close it.
    fd = os.dup(root.root_fd)
    try:
        stopped = walk(fd, root, "", 0)
    finally:
        os.close(fd)
    return found, incomplete or stopped == "limit"


def _read_contains(
    path: _File,
    needle: bytes,
    rule_max: int,
    max_file: int,
    max_total: int,
    state: dict,
    cancel=None,
) -> bool:
    # O_NONBLOCK: a file swapped for a FIFO after the scan must not block the open.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        parent = path.parent.open()
    except OSError:
        state["incomplete"] = True
        return False
    try:
        handle = os.open(path.name, flags, dir_fd=parent)
    except OSError:
        state["incomplete"] = True
        return False
    finally:
        os.close(parent)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            state["incomplete"] = True
            return False
        # The enumeration's size may be stale (a log can grow between the scan and the
        # read), so the budget comes from the file as it is now. Only the rule's own cap
        # and the totals bound it; a zero-length file is still opened and read.
        intended = min(rule_max, info.st_size)
        budget = min(intended, max_file, max_total - state["bytes_read"])
        if budget < intended:
            state["incomplete"] = True
        chunks: list[bytes] = []
        remaining = max(0, budget)
        while remaining > 0:
            if cancel is not None and cancel.is_set():
                state["cancelled"] = True
                break
            chunk = os.read(handle, min(1 << 20, remaining))
            if not chunk:
                state["incomplete"] = True
                break
            chunks.append(chunk)
            state["bytes_read"] += len(chunk)
            remaining -= len(chunk)
        if os.fstat(handle).st_size > info.st_size:
            # It kept growing while it was read: what is past the budget was never looked at.
            state["incomplete"] = True
        return needle in b"".join(chunks)
    except OSError:
        state["incomplete"] = True
        return False
    finally:
        os.close(handle)


def _rule_count(
    rule: dict, entries, state: dict, max_file_bytes: int, max_total: int, cancel=None
) -> int:
    kind = rule["type"]
    pattern = rule["glob"]
    hits = 0
    if kind == "path_exists":
        for rel, _mode, _size, _path in entries:
            if cancel is not None and cancel.is_set():
                state["cancelled"] = True
                break
            if fnmatch.fnmatchcase(rel, pattern):
                hits += 1
        return hits
    if kind == "file_size":
        for rel, mode, size, _path in entries:
            if cancel is not None and cancel.is_set():
                state["cancelled"] = True
                break
            if stat.S_ISREG(mode) and fnmatch.fnmatchcase(rel, pattern):
                if rule["min"] <= size <= rule["max"]:
                    hits += 1
        return hits
    needle = rule["text"].encode("utf-8")
    for rel, mode, _size, path in entries:
        if cancel is not None and cancel.is_set():
            state["cancelled"] = True
            break
        if not stat.S_ISREG(mode) or not fnmatch.fnmatchcase(rel, pattern):
            continue
        if _read_contains(
            path, needle, rule["max_bytes"], max_file_bytes, max_total, state, cancel
        ):
            hits += 1
    return hits


def check_profile(
    name: str,
    profile: dict,
    indicators: list[Indicator] | None,
    *,
    cancel=None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = TOTAL_READ_CAP,
) -> SafetyResult:
    """Check one profile's prefix and install folder against the list.

    ``name`` is the profile's config key: a Wine profile that never set ``prefix`` is still
    checked at its backend's ``<profile folder>/prefix``. ``indicators is None`` means no
    verified list exists: the check looks at nothing.
    """
    if indicators is None:
        return SafetyResult(level=NO_LIST)

    kind = str(profile.get("backend") or "")
    rules = [indicator for indicator in indicators if indicator.applies_to(kind)]
    with ExitStack() as handles:
        entries = []
        roots = _roots(profile, name)
        truncated = not roots
        for root in roots:
            try:
                directory = _open_root(root, handles)
                found, cut = _entries(directory, cancel, max_files - len(entries))
            except OSError:
                truncated = True
                continue
            entries.extend(found)
            truncated = truncated or cut

        state = {"bytes_read": 0, "incomplete": truncated}
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")

        def cancelled(matches: tuple[Match, ...] = ()) -> SafetyResult:
            return SafetyResult(
                level=CANCELLED,
                matches=matches,
                checked_at=checked_at,
                files_visited=len(entries),
                bytes_read=state["bytes_read"],
                truncated=state["incomplete"],
                cancelled=True,
            )

        if cancel is not None and cancel.is_set():
            return cancelled()

        matches: list[Match] = []
        for indicator in rules:
            if cancel is not None and cancel.is_set():
                return cancelled(tuple(matches))
            count = _rule_count(
                indicator.rule, entries, state, max_file_bytes, max_total_bytes, cancel
            )
            if count:
                matches.append(Match(indicator.id, count, indicator.level))
            if state.get("cancelled"):
                return cancelled(tuple(matches))

        # Cancellation may have landed during the last rule or between rules.
        if cancel is not None and cancel.is_set():
            return cancelled(tuple(matches))

        if any(match.level == COMPROMISED for match in matches):
            level = COMPROMISED
        elif matches:
            level = EXPOSED
        else:
            level = INCOMPLETE if state["incomplete"] else CLEAN
        return SafetyResult(
            level=level,
            matches=tuple(matches),
            checked_at=checked_at,
            files_visited=len(entries),
            bytes_read=state["bytes_read"],
            truncated=state["incomplete"],
        )


def result_summary(result: SafetyResult) -> str:
    """A short plain answer. It names indicator ids and counts, never file content."""
    if result.level == NO_LIST:
        return (
            "This check cannot run yet: Nadare has no list of known bad files. "
            "This is not a problem with the game, and there is nothing for you to do."
        )
    if result.level == CANCELLED or result.cancelled:
        return "Safety check cancelled."
    if result.level == ERROR:
        return "The safety check could not run."
    if result.level == INCOMPLETE:
        previous = (
            " A previous check found signs of compromise." if result.compromise_warning else ""
        )
        return (
            "Incomplete — some files were skipped or could not be read. Run the check again."
            + previous
        )
    if result.truncated and result.level in LEVELS:
        return (
            result_summary(replace(result, truncated=False))
            + " This check was incomplete; some files were skipped."
        )
    if result.level == CLEAN:
        return (
            "Clean — nothing in this game's files matched the current indicator list. "
            "This is weaker than it sounds: reinstalling the game can wipe old evidence."
        )
    found = ", ".join(f"{match.id} ({match.count})" for match in result.matches)
    if result.level == COMPROMISED:
        return (
            "Compromised — signs that this game's files were used to run malicious code. "
            f"Matched: {found}."
        )
    return f"Exposed — signs that something bad ran. Matched: {found}."


# ---- per-profile storage ---------------------------------------------------------------------
def results_path() -> Path:
    return paths.state_dir() / "safety" / "results.json"


def _store_path(path: str | os.PathLike[str] | None) -> Path:
    return Path(path) if path is not None else results_path()


def _read_store(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if len(raw) > 512 * 1024:
            return {}
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return profiles if isinstance(profiles, dict) else {}


def _record(result: SafetyResult, *, dismissed: bool = False) -> dict:
    return {
        "level": result.level,
        "matches": [
            {"id": match.id, "count": match.count, "level": match.level}
            for match in result.matches
        ],
        "checked_at": result.checked_at,
        "files_visited": result.files_visited,
        "bytes_read": result.bytes_read,
        "truncated": result.truncated,
        "cancelled": result.cancelled,
        "dismissed": dismissed,
        "compromise_warning": result.compromise_warning,
    }


def _int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _result_from(record: Any) -> SafetyResult | None:
    if not isinstance(record, dict):
        return None
    level = record.get("level")
    if level not in (*LEVELS, CLEAN, NO_LIST, CANCELLED, INCOMPLETE, ERROR):
        return None
    matches: list[Match] = []
    for row in record.get("matches") or []:
        if not isinstance(row, dict):
            continue
        identifier, count, match_level = row.get("id"), row.get("count"), row.get("level")
        if isinstance(identifier, str) and type(count) is int and match_level in LEVELS:
            matches.append(Match(identifier, count, match_level))
    checked_at = record.get("checked_at")
    if not isinstance(checked_at, str):
        return None
    return SafetyResult(
        level=level,
        matches=tuple(matches),
        checked_at=checked_at,
        files_visited=_int(record.get("files_visited")),
        bytes_read=_int(record.get("bytes_read")),
        truncated=bool(record.get("truncated")),
        cancelled=bool(record.get("cancelled")),
        dismissed=bool(record.get("dismissed")),
        compromise_warning=bool(record.get("compromise_warning")),
    )


def save_result(profile: str, result: SafetyResult, *, path=None) -> None:
    """Remember one profile's newest result (a new result is never already dismissed)."""
    target = _store_path(path)
    # Read and replace under one lock: two instances sharing the store must not each write
    # back a snapshot taken before the other's result (atomic replace alone does not
    # serialize the read-modify-write).
    with _list_lock(target):
        profiles = _read_store(target)
        previous = _result_from(profiles.get(profile))
        if previous and (
            result.truncated or result.level in (INCOMPLETE, NO_LIST, ERROR, CANCELLED)
        ):
            warning = (
                previous.level == COMPROMISED or previous.compromise_warning
            ) and not previous.dismissed
            result = replace(result, compromise_warning=warning)
        profiles[profile] = _record(result)
        atomicfile.write(
            target,
            json.dumps({"version": 1, "profiles": profiles}),
            make_parent=True,
            sync_dir=True,
        )


def load_result(profile: str, *, path=None) -> SafetyResult | None:
    return _result_from(_read_store(_store_path(path)).get(profile))


def load_all_results(*, path=None) -> dict[str, SafetyResult]:
    results: dict[str, SafetyResult] = {}
    for profile, record in _read_store(_store_path(path)).items():
        result = _result_from(record)
        if result is not None:
            results[profile] = result
    return results


def dismiss(profile: str, *, path=None) -> None:
    """Keep the newest result but stop showing its banner."""
    target = _store_path(path)
    with _list_lock(target):
        profiles = _read_store(target)
        record = profiles.get(profile)
        if not isinstance(record, dict):
            return
        record["dismissed"] = True
        atomicfile.write(
            target,
            json.dumps({"version": 1, "profiles": profiles}),
            make_parent=True,
            sync_dir=True,
        )


def system_check_line(*, path=None) -> str:
    """One plain line for the System check window."""
    results = load_all_results(path=path)
    if not results:
        return "Safety check: not run yet."
    labels = {
        CLEAN: "Clean",
        INCOMPLETE: "Incomplete",
        EXPOSED: "Exposed",
        COMPROMISED: "Compromised",
        NO_LIST: "No indicator list yet",
        CANCELLED: "Cancelled",
    }
    parts = []
    for profile, result in sorted(results.items()):
        when = result.checked_at[:10]
        label = labels.get(result.level, result.level)
        if result.truncated and result.level != INCOMPLETE:
            label += " (incomplete)"
        if result.compromise_warning:
            label += "; previous compromise warning"
        parts.append(f"{profile}: {label} ({when})")
    return "Safety check: " + "; ".join(parts) + "."


__all__ = [
    "CANCELLED",
    "CLEAN",
    "COMPROMISED",
    "DEFAULT_MAX_FILE_BYTES",
    "EXPOSED",
    "Indicator",
    "INCOMPLETE",
    "MAX_FILES",
    "MAX_INDICATORS",
    "Match",
    "NO_LIST",
    "SafetyResult",
    "TOTAL_READ_CAP",
    "check_profile",
    "dismiss",
    "iocs_serial_path",
    "indicators_cache_path",
    "load_all_results",
    "load_indicators",
    "load_result",
    "parse_indicators",
    "result_summary",
    "results_path",
    "save_result",
    "system_check_line",
    "verified_indicators",
]
