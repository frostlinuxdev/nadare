# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Signed tested-runner pins. Lookups are local; only refresh() uses the network.

The saved signed body doubles as the replay watermark. A failed refresh keeps the
last accepted body usable, so a replayed or older file (or an outage) cannot drop a
user back to the built-in floor. Atomic replacement and a process lock make both
successful fetches and failures subject to the same six-hour backoff.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import __version__
from . import atomicfile, hosting, indexfetch, paths, releasesig, updatecheck

log = logging.getLogger("nadare.runners")
REFRESH_SECONDS = 6 * 3600
# Allow for JSON string escaping of the 64 KiB body, 16 KiB signature,
# duplicated skipped family names and failure details, including older ASCII saves.
_MAX_STATE_BYTES = 1024 * 1024
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Change:
    family: str
    previous_tag: str
    tag: str
    critical: bool


def cache_path() -> Path:
    """Keep the replay watermark in state, separate from disposable download caches."""
    source = hashlib.sha256(" ".join(hosting.index_bases()).encode()).hexdigest()[:16]
    return paths.state_dir() / "runner-index" / f"{source}.json"


def _read() -> dict:
    try:
        raw = cache_path().read_bytes()
        if len(raw) > _MAX_STATE_BYTES:
            return {}
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _asset(spec, tag: str, file_name: Any = None) -> str:
    """The archive name for ``tag``: the entry's ``file``, else the built-in derivation."""
    if not spec.tested or not spec.pinned_asset:
        raise ValueError("family has no built-in pin")
    if file_name is not None:
        name = plain_file_name(file_name)
        if tag not in name:
            raise ValueError("runner file does not name its tag")
        return name
    name = spec.pinned_asset[0].replace(spec.tested[0], tag)
    if not re.fullmatch(spec.asset, name) or tag not in name:
        raise ValueError("invalid runner tag")
    return name


#: Archive types a manifest ``file`` may name.
_ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.zst")


def plain_file_name(value: Any) -> str:
    """A safe manifest ``file`` value, or ``ValueError``.

    The name becomes one path component under the download cache, so it must not be able
    to leave that folder. It must also be a runner archive.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or "/" in value
        or "\0" in value
        or ".." in value
        or value.startswith(".")
        or not value.endswith(_ARCHIVE_SUFFIXES)
    ):
        raise ValueError("invalid runner file name")
    return value


def _numbers(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def _version_key(tag: str) -> tuple[tuple[int, ...], int, int]:
    """Order numbered prereleases before their final, retaining runner build numbers."""
    prerelease = re.search(r"[-._]?(alpha|beta|preview|pre|rc)[-._]?(\d*)$", tag, re.I)
    if prerelease:
        stage = {"alpha": 0, "beta": 1, "preview": 2, "pre": 2, "rc": 3}
        return (_numbers(tag[:prerelease.start()]), stage[prerelease[1].lower()],
                int(prerelease[2] or 0))
    return _numbers(tag), 4, 0


def _verified(envelope: Any) -> tuple[dict, list[str]]:
    from .runners import RUNNERS

    if not isinstance(envelope, dict):
        raise ValueError("missing signed body")
    body, signature = envelope["body"], envelope["signature"]
    if not isinstance(body, str) or len(body.encode()) > 65536:
        raise ValueError("invalid body")
    if not isinstance(signature, str) or len(signature) > 16384:
        raise ValueError("invalid signature")
    # Verify the exact bytes before parsing or using any manifest fields, just as
    # selfupdate.verify_signature does. No gpg process or new trust anchor.
    with tempfile.TemporaryDirectory(prefix="nadare-runner-signature-") as folder:
        target = Path(folder) / "runners.json"
        target.write_bytes(body.encode("utf-8"))
        releasesig.verify_detached(target, signature)
    doc = json.loads(body, object_pairs_hook=_unique)
    if not isinstance(doc, dict) or type(doc.get("epoch")) is not int or doc["epoch"] < 1:
        raise ValueError("invalid epoch")
    rows = doc.get("runners")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("invalid runner families")
    # A newer manifest may name a family this app does not have: skip that entry and
    # still apply the rest. Any problem with a known family rejects the whole file.
    skipped = sorted(set(rows) - set(RUNNERS))
    known = {family: row for family, row in rows.items() if family in RUNNERS}
    for family, row in known.items():
        if not isinstance(row, dict):
            raise ValueError("invalid runner entry")
        spec = RUNNERS[family]
        tag = row["tag"]
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", tag):
            raise ValueError("invalid tag")
        _asset(spec, tag, row.get("file"))
        digest = row["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid sha256")
        if type(row["size"]) is not int or row["size"] <= 0:
            raise ValueError("invalid archive size")
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("published_at needs a timezone")
        tested = row["tested"]
        if not isinstance(tested, dict) or type(tested.get("yes")) is not bool:
            raise ValueError("invalid test result")
        if not isinstance(tested.get("date"), str):
            raise ValueError("invalid test date")
        date.fromisoformat(tested["date"])
        if tested["yes"] or "report" in tested:
            report = urlparse(str(tested.get("report") or ""))
            if report.scheme != "https" or not report.hostname:
                raise ValueError("invalid test report link")
        if type(row.get("critical", False)) is not bool:
            raise ValueError("invalid critical flag")
        if "min_nadare" in row and updatecheck.version_key(row["min_nadare"]) is None:
            raise ValueError("invalid min_nadare")
        if "previous_tag" in row and not isinstance(row["previous_tag"], str):
            raise ValueError("invalid previous tag")
        floor, floor_hash = spec.tested
        if _version_key(tag) < _version_key(floor):
            raise ValueError("runner is older than the built-in pin")
        if _version_key(tag) == _version_key(floor) and (
            tag != floor
            or digest != floor_hash
            or row["size"] != spec.pinned_asset[1]
            or (row.get("file") is not None and row["file"] != spec.pinned_asset[0])
        ):
            raise ValueError("manifest changes the built-in pin")
    return {**doc, "runners": known}, skipped


def _eligible(row: dict) -> bool:
    minimum = row.get("min_nadare")
    return row["tested"]["yes"] and (
        minimum is None or updatecheck.version_key(__version__) >= updatecheck.version_key(minimum)
    )


def _accepted(state: dict) -> dict | None:
    try:
        doc, _skipped = _verified(state.get("accepted"))
        return doc
    except Exception:  # noqa: BLE001 - any bad saved input means the built-in pin
        return None


def effective(spec):
    """The effective RunnerSpec, without a network request or a profile write."""
    if not hosting.index_bases():
        return spec
    state = _read()
    if not state.get("usable"):
        return spec
    doc = _accepted(state)
    row = doc["runners"].get(spec.id) if doc else None
    if not row or not _eligible(row):
        return spec
    return replace(spec, tested=(row["tag"], row["sha256"]),
                   pinned_asset=(_asset(spec, row["tag"], row.get("file")), row["size"]))


def refresh() -> list[Change]:
    """Refresh at most once per six hours, including failures; return new announcements.

    Mirrors must supply a matching body/signature pair. A bad pair rejects the
    whole refresh; it cannot be combined with a signature from another mirror.
    """
    bases = hosting.index_bases()
    if not bases:
        return []
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _refresh_locked(bases, path)
    except OSError as exc:
        log.debug("cannot refresh runner recommendations: %s", exc)
        return []


def _refresh_locked(bases: list[str], path: Path) -> list[Change]:
    from .runners import RUNNERS

    state = _read()
    now = time.time()
    last = state.get("attempt")
    if isinstance(last, (int, float)) and 0 <= now - last < REFRESH_SECONDS:
        return []
    prior = _accepted(state)
    state.update(attempt=now)
    # Keep the last accepted body usable through a failed check: a replayed old
    # file or an outage must not downgrade the user to the built-in floor.
    state["usable"] = prior is not None
    changes = []
    try:
        envelope = doc = skipped = None
        for base in bases:
            if not base.startswith("https://"):
                continue
            try:
                body = indexfetch.open_bytes(base + "runners.json")
                signature = indexfetch.open_bytes(base + "runners.json.asc", max_bytes=16384)
            except Exception:  # noqa: BLE001 - transport outages may try the next mirror
                continue
            envelope = {"body": body.decode("utf-8"), "signature": signature.decode("ascii")}
            doc, skipped = _verified(envelope)
            break
        if doc is None:
            raise ValueError("index unavailable")
        if prior:
            if doc["epoch"] < prior["epoch"]:
                raise ValueError("replayed epoch")
            if doc["epoch"] == prior["epoch"] and envelope != state["accepted"]:
                raise ValueError("changed body at the same epoch")
            for family, previous in prior["runners"].items():
                row = doc["runners"].get(family)
                if _eligible(previous) and (row is None or not _eligible(row)):
                    raise ValueError("removing an accepted pin needs an explicit rollback")
                if row and row["tag"] == previous["tag"] and (
                    any(row[field] != previous[field] for field in ("sha256", "size"))
                    or _asset(RUNNERS[family], row["tag"], row.get("file"))
                    != _asset(RUNNERS[family], previous["tag"], previous.get("file"))
                ):
                    raise ValueError("manifest changes an accepted build")
        for family, row in doc["runners"].items():
            spec = RUNNERS[family]
            previous = prior["runners"].get(family) if prior else None
            previous_tag = previous["tag"] if previous and _eligible(previous) else spec.tested[0]
            if _version_key(row["tag"]) < _version_key(previous_tag) and (
                not prior or doc["epoch"] <= prior["epoch"]
                or row.get("previous_tag") != previous_tag
            ):
                raise ValueError("rollback needs a higher epoch and the previous tag")
            if _eligible(row) and row["tag"] != previous_tag:
                changes.append(Change(family, previous_tag, row["tag"], row.get("critical", False)))
        if skipped and skipped != state.get("skipped"):
            log.info(
                "Ignoring runners.json entries for unknown runner families: %s",
                ", ".join(skipped),
            )
        state.update(accepted=envelope, usable=True, error=None, skipped=skipped)
        atomicfile.write(path, json.dumps(state, ensure_ascii=False), sync_dir=True)
    except Exception as exc:  # noqa: BLE001 - keep the floor on every validation/transport failure
        reason = str(exc)
        if state.get("error") != reason:
            log.warning("Ignoring runners.json: %s", reason)
        state["error"] = reason
        state["usable"] = prior is not None
        atomicfile.write(path, json.dumps(state, ensure_ascii=False), sync_dir=True)
        return []
    for change in changes:
        log.info("Recommended runner updated to %s", change.tag)
    return changes


def critical_offers(change: Change, profiles: dict) -> list[str]:
    """Names to offer a critical update to; this never changes a profile or running process."""
    from . import runners

    if not change.critical:
        return []
    return [name for name, profile in profiles.items()
            if (build := runners._named_build(profile))
            and runners.matches_tag(build, change.previous_tag)]
