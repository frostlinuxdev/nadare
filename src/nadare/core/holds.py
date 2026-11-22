# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A signed list of games the maintainers have marked dangerous to launch (#805).

The feed is ``<index base>/holds.json``, an envelope like the runner index::

    {"body": "<exact JSON text>", "signature": "<detached OpenPGP signature>"}

The signature is checked against the release key before the body is parsed. A missing,
bad or unsigned body is ignored; the last verified document from the index cache is kept,
so a network outage or a hijacked index cannot switch launches off. The body also carries a
required ``serial``: the highest accepted one is saved under the state dir together with the
signed body and its hash, and a replayed older list (or a changed body at the same serial) is
refused. The saved body is the durable fallback when the index cache write did not land. The
list is text only and can only refuse Play: install, update, repair and stop are never touched.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from . import atomicfile, hosting, indexfetch, notices, paths, process, releasesig, updatecheck

log = logging.getLogger(__name__)

CACHE_REL = "holds.json"
SERIAL_FILE = "holds-serial.json"
LOCK_FILE = "holds-serial.lock"
#: The saved record holds the accepted signed body too, so it allows for JSON escaping.
MAX_STATE = 512 * 1024
_SHA256_RE = re.compile(r"[a-f0-9]{64}")
FRESH_SECONDS = 6 * 3600
BACKOFF_SECONDS = 3600
MAX_HOLDS = 20
MAX_REASON = 280
MAX_BYTES = 64 * 1024
MAX_BODY = 64 * 1024
MAX_SIGNATURE = 16 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_KIND_RE = re.compile(r"[a-z0-9-]{1,48}")

#: The one quiet line the System check shows after a rejected signed list ("" when none).
_LAST_PROBLEM: str | None = None

#: The last answer this process got, so Play does not fetch again on every click. Only the
#: real network path uses it; a test that injects ``opener`` always reads the feed.
_MEMO: tuple[float, list[Hold], float] | None = None  # (fetched_at, holds, valid_for)

#: Serialises the saved-state transaction between threads of this process (a file lock
#: covers other Nadare processes).
_LOCK = threading.Lock()

#: Serialises default-path loads from the memo check through publishing the new memo, so a
#: load that started earlier cannot overwrite an answer a later load already published.
_LOAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class Hold:
    id: str
    kinds: tuple[str, ...]
    below: str | None  # applies only to app versions older than this
    reason: str
    link: str | None


def _clean_text(value: Any) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:MAX_REASON]


def _one(raw: Any, feed: str) -> Hold | None:
    if not isinstance(raw, dict):
        return None
    hid = raw.get("id")
    if not isinstance(hid, str) or not _ID_RE.fullmatch(hid):
        return None
    kinds = raw.get("kinds")
    if (
        not isinstance(kinds, list)
        or not kinds
        or len(kinds) > 20
        or not all(isinstance(kind, str) and _KIND_RE.fullmatch(kind) for kind in kinds)
    ):
        return None
    below = raw.get("below")
    if below is not None and (
        not isinstance(below, str) or updatecheck.version_key(below) is None
    ):
        return None
    reason = _clean_text(raw.get("reason") or "")
    if not reason:
        return None
    return Hold(
        hid,
        tuple(kinds),
        below,
        reason,
        notices.link_allowed(raw.get("link"), feed),
    )


def parse(document: Any, *, url: str = "") -> list[Hold]:
    """The valid holds in ``document`` (at most :data:`MAX_HOLDS`, ids unique)."""
    rows = document.get("holds") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[Hold] = []
    seen: set[str] = set()
    for raw in rows:
        item = _one(raw, url)
        if item is not None and item.id not in seen:
            seen.add(item.id)
            found.append(item)
        if len(found) >= MAX_HOLDS:
            break
    return found


def _verified(envelope: Any) -> tuple[dict[str, Any], str]:
    """Check and return the signed body and its sha256, before any field is used."""
    if not isinstance(envelope, dict):
        raise ValueError("missing signed body")
    body, signature = envelope.get("body"), envelope.get("signature")
    if not isinstance(body, str) or len(body.encode()) > MAX_BODY:
        raise ValueError("invalid body")
    if not isinstance(signature, str) or len(signature) > MAX_SIGNATURE:
        raise ValueError("invalid signature")
    payload = body.encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="nadare-hold-signature-") as folder:
        target = Path(folder) / "holds.json"
        target.write_bytes(payload)
        releasesig.verify_detached(target, signature)
    document = json.loads(body)
    if not isinstance(document, dict) or type(document.get("version")) is not int:
        raise ValueError("invalid holds document")
    if document["version"] != 1 or not isinstance(document.get("holds"), list):
        raise ValueError("invalid holds document")
    if type(document.get("serial")) is not int or document["serial"] < 1:
        raise ValueError("invalid serial")
    return document, hashlib.sha256(payload).hexdigest()


def serial_path() -> Path:
    """The saved replay watermark: the highest accepted serial, its body hash and the body."""
    return paths.state_dir() / SERIAL_FILE


def lock_path() -> Path:
    """The file whose ``flock`` serialises the saved-state transaction across processes."""
    return paths.state_dir() / LOCK_FILE


def _read_state() -> dict[str, Any] | None:
    """The saved accepted record, or ``None`` when it is missing or unreadable."""
    path = serial_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("cannot read the saved holds serial: %s", exc)
        return None
    try:
        if len(raw) > MAX_STATE:
            raise ValueError("state file too large")
        state = json.loads(raw)
        serial, digest = state["serial"], state["sha256"]
        if type(serial) is not int or serial < 1:
            raise ValueError("invalid serial")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid body hash")
        envelope = {"body": state["body"], "signature": state["signature"]}
        if not isinstance(envelope["body"], str) or not isinstance(envelope["signature"], str):
            raise ValueError("invalid accepted body")
        return {"serial": serial, "sha256": digest, "envelope": envelope}
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ignoring the saved holds serial: %s", exc)
        return None


def _write_state(serial: int, digest: str, envelope: Mapping[str, Any]) -> None:
    """Save the accepted serial, its body hash and the signed body in one atomic record."""
    record = {
        "serial": serial,
        "sha256": digest,
        "body": envelope["body"],
        "signature": envelope["signature"],
    }
    try:
        atomicfile.write(
            serial_path(),
            json.dumps(record),
            make_parent=True,
            sync_dir=True,
        )
    except OSError as exc:
        # A read-only state folder must not switch holds off; the next accepted copy retries.
        log.warning("cannot save the holds serial: %s", exc)


def _checked_state() -> tuple[dict[str, Any], str] | None:
    """The saved record and its verified body hash, or ``None`` when it does not verify.

    The outer serial, hash and envelope are only a cache of an accepted document: a saved
    record that was edited, truncated or corrupted must not be trusted as the replay
    watermark. Checking it again here keeps a bad record from refusing every real feed.
    """
    state = _read_state()
    if state is None:
        return None
    try:
        document, digest = _verified(state["envelope"])
        if digest != state["sha256"] or document["serial"] != state["serial"]:
            raise ValueError("the saved body does not match its serial")
    except Exception as exc:  # noqa: BLE001 - a bad saved body must not block a launch
        log.warning("ignoring the saved hold list: %s", exc)
        return None
    return document, digest


def _accepted_document() -> dict[str, Any] | None:
    """The last accepted document from the saved record, checked again, or ``None``.

    The record is the durable copy of what was accepted: ``indexfetch`` may have failed to
    save its own cache copy, or a replayed cache entry may be refused, and the accepted
    holds must still be there offline.
    """
    checked = _checked_state()
    return None if checked is None else checked[0]


@contextlib.contextmanager
def _state_lock():
    """Guard the saved-state read, comparison and write, across threads and processes.

    Atomic replacement alone stops a torn file but not two acceptances that both read the
    old serial and then write in the wrong order. The lock is held only for this small
    local transaction; no network call may happen inside it.
    """
    with _LOCK:
        path = lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # A state folder that cannot be locked still gets the in-process lock.
            log.debug("cannot lock the saved holds state: %s", exc)
            yield
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                if not process.flock_unavailable(exc):
                    log.warning("cannot lock the saved holds state: %s", exc)
            yield
        finally:
            os.close(fd)  # closing the descriptor drops the flock


def _accept(envelope: Any) -> dict[str, Any]:
    """Verify a signed body, refuse a replay, and save the accepted body with its serial.

    Like the runner index's epoch: an older serial is refused, and an equal serial may
    not carry a different body (a mirror could otherwise swap the list at the same serial).
    The saved record doubles as the last accepted copy, so advancing the watermark can
    never lose a hold that was just accepted.
    """
    document, digest = _verified(envelope)
    serial = document["serial"]
    with _state_lock():
        saved = _checked_state()
        if saved is not None:
            saved_document, saved_digest = saved
            if serial < saved_document["serial"]:
                raise ValueError("an older hold list was replayed")
            if serial == saved_document["serial"]:
                if digest != saved_digest:
                    raise ValueError("the hold list changed at the same serial")
                return document
        _write_state(serial, digest, envelope)
    return document


def applies(hold: Hold, kind: str, version: str = __version__) -> bool:
    """Whether ``hold`` targets this backend ``kind`` at this app ``version``."""
    if kind not in hold.kinds:
        return False
    if hold.below is None:
        return True
    have, limit = updatecheck.version_key(version), updatecheck.version_key(hold.below)
    return have is not None and limit is not None and have < limit


def load(*, opener: Any = None) -> list[Hold]:
    """The current signed holds; never raises.

    Mirrors are tried in the same order and through the same hardened opener as the index.
    A bad or unsigned document falls back to the last verified copy; without one the result
    is empty (fail-open for launching).
    """
    if opener is not None:
        return _load(opener)
    # Two default-path loads run at start-up and on the launch worker. Serialise them from
    # the memo check through publishing the new answer: an older fetch that finishes last
    # must not replace a newer answer that this process already published.
    with _LOAD_LOCK:
        return _load(None)


def _load(opener: Any) -> list[Hold]:
    global _LAST_PROBLEM, _MEMO
    now = time.time()
    if opener is None and _MEMO is not None and 0 <= now - _MEMO[0] < _MEMO[2]:
        return list(_MEMO[1])
    bases = [base for base in hosting.index_bases() if base.startswith("https://")]
    if not bases:
        _LAST_PROBLEM = None
        # Disabling fetching is not the same as disabling checks: an accepted hold must
        # still refuse Play, exactly as it does when the mirrors are unreachable.
        document = _accepted_document()
        return [] if document is None else parse(document)
    state: dict[str, Any] = {"doc": None, "rejected": None}

    def validate(envelope: Any) -> bool:
        try:
            state["doc"] = _accept(envelope)
        except Exception as exc:  # noqa: BLE001 - any rejected document is ignored
            state["rejected"] = str(exc)
            return False
        return True

    try:
        result = indexfetch.fetch_json(
            CACHE_REL,
            validate=validate,
            opener=opener,
            max_bytes=MAX_BYTES,
        )
    except indexfetch.IndexUnavailable as exc:
        log.debug("holds unavailable: %s", exc)
        # The saved record holds the last accepted signed body, so a cache write that
        # failed (or a replaying mirror) cannot lose a hold that was already accepted.
        document = _accepted_document()
        _LAST_PROBLEM = (
            _problem_text(state, cached=document is not None) if state["rejected"] else None
        )
        if document is not None:
            items = parse(document, url=bases[0] + CACHE_REL)
            if opener is None:
                _MEMO = (now, list(items), BACKOFF_SECONDS)
            return items
        if opener is None:
            _MEMO = (now, [], BACKOFF_SECONDS)
        return []
    except Exception:  # noqa: BLE001 - a hold list must never get in the way
        log.debug("holds fetch failed", exc_info=True)
        _LAST_PROBLEM = None
        if opener is None:
            _MEMO = (now, [], BACKOFF_SECONDS)
        return []
    document = state["doc"]
    if document is None:  # defensive: the chosen payload was not seen by validate()
        document = _accept(result.data)
    # A rejected newer document is worth one quiet line in the System check. A plain
    # network failure is silent, as it is for notices.
    _LAST_PROBLEM = _problem_text(state) if result.from_cache and state["rejected"] else None
    feed = result.source or (bases[0] + CACHE_REL)
    items = parse(document, url=feed)
    if opener is None:
        _MEMO = (now, list(items), FRESH_SECONDS)
    return items


def known() -> list[Hold]:
    """The holds already known to this process, without any network request.

    The memo from a completed :func:`load`, else the last verified copy from the index
    cache, else the last accepted body saved with the watermark. Signature and serial are
    checked again on both. The GUI asks this on the window thread before Play, where a
    fetch could freeze for its timeout.
    """
    if _MEMO is not None:
        return list(_MEMO[1])
    document = None
    accepted: dict[str, Any] = {"doc": None}

    def validate(envelope: Any) -> bool:
        try:
            accepted["doc"] = _accept(envelope)
        except Exception:  # noqa: BLE001 - a saved copy that no longer verifies is ignored
            log.debug("saved holds are unusable", exc_info=True)
            return False
        return True

    try:
        # Validating inside the mirror loop lets an unusable copy on mirror 1 fall through
        # to a good copy on mirror 2, as the online path already does.
        if indexfetch.read_cached(CACHE_REL, validate=validate) is not None:
            document = accepted["doc"]
    except Exception:  # noqa: BLE001 - a saved copy that no longer verifies is ignored
        log.debug("saved holds are unusable", exc_info=True)
    if document is None:
        document = _accepted_document()
    if document is None:
        return []
    base = next((base for base in hosting.index_bases() if base.startswith("https://")), "")
    return parse(document, url=(base + CACHE_REL) if base else "")


def _problem_text(state: dict[str, Any], *, cached: bool = True) -> str:
    reason = state.get("rejected") or "the document was rejected"
    log.debug("ignoring holds.json: %s", reason)
    if cached:
        return (
            "a hold list failed its signature, format or replay check; "
            "the last good copy is used"
        )
    return "a hold list failed its signature, format or replay check; launches are not blocked"


def last_problem() -> str | None:
    """The System check line for the last :func:`load`, or ``None``."""
    return _LAST_PROBLEM


def system_check_line() -> str | None:
    """One quiet line for the System check, or ``None`` when the feed is fine."""
    load()
    problem = last_problem()
    if problem is None:
        return None
    return f"holds: {problem}"


def _acknowledged(data: Mapping[str, Any]) -> list[str]:
    """The hold ids this profile already accepted, in the old string or the new list form."""
    value = data.get("hold_override")
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def acknowledge(data: Mapping[str, Any], hold_id: str) -> list[str]:
    """The profile's saved acknowledgements plus ``hold_id``, oldest first."""
    ids = _acknowledged(data)
    if hold_id not in ids:
        ids.append(hold_id)
    return ids[-MAX_HOLDS:]


def blocking_hold(
    config: Any,
    profile: str,
    kind: str | None = None,
    *,
    version: str = __version__,
    holds: list[Hold] | None = None,
) -> Hold | None:
    """The first hold that stops this profile, or ``None``.

    The Settings switch turns every hold off. ``hold_override`` remembers the hold ids
    this profile accepted: a new id asks again, and another applicable hold still blocks.
    """
    if not config.data.get("check_holds", True):
        return None
    try:
        data = config.profile(profile)
    except KeyError:
        return None
    kind = kind if kind is not None else str(data.get("backend") or "")
    if not kind:
        return None
    override = set(_acknowledged(data))
    for hold in load() if holds is None else holds:
        if hold.id not in override and applies(hold, kind, version):
            return hold
    return None


__all__ = [
    "Hold",
    "MAX_HOLDS",
    "acknowledge",
    "applies",
    "blocking_hold",
    "known",
    "last_problem",
    "load",
    "lock_path",
    "parse",
    "serial_path",
    "system_check_line",
]
