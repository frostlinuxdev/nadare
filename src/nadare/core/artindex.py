# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Curated cover art, published in the index as ``catalog/art.json``.

Many of the best official cover URLs carry a content hash in their name and change when the
publisher updates the art. Keeping the list in the index means a dead URL is fixed with a
commit instead of a release. The app treats the document as untrusted: it only accepts https
URLs on the hosts allowed for that game *here in code* (the index can never widen that list),
keeps at most a few candidates per game, and falls back to the sources it always had.

Format::

    {"schema": 1, "updated": "2026-10-28",
     "games": {"direct-genshin": {"cover": [
         {"url": "https://...", "orientation": "wide", "width": 2560, "height": 1440}]}}}
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_CANDIDATES = 4
MAX_DIMENSION = 20_000
#: How long a loaded document is used, and how long a failed load is not retried.
TTL = 6 * 3600.0
RETRY_AFTER_FAILURE = 300.0

STEAM_HOSTS = (
    "cdn.cloudflare.steamstatic.com",
    "shared.akamai.steamstatic.com",
    "shared.fastly.steamstatic.com",
    "shared.cloudflare.steamstatic.com",
)
_EPIC = ("cdn2.unrealengine.com",)

#: The hosts each game's index art may be served from. Anything else is dropped.
ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "direct-genshin": _EPIC,
    "direct-hsr": _EPIC,
    "direct-hi3": _EPIC,
    "direct-zzz": STEAM_HOSTS,
    "direct-wuwa": STEAM_HOSTS,
    "direct-pgr": STEAM_HOSTS,
    "direct-gfl2": STEAM_HOSTS,
    "direct-gfl2-haoplay": STEAM_HOSTS,
    "launcher-gfl2-haoplay": STEAM_HOSTS,
    "direct-endfield": STEAM_HOSTS,
    "stellasora-yostar": STEAM_HOSTS,
    "direct-nte": ("nte.perfectworld.com",),
    "launcher-nte": ("nte.perfectworld.com",),
    "launcher-nikke": ("nikke-en.com",),
    "direct-aethergazer": ("www.aethergazer.com",),
    "direct-bd2": ("www.browndust2.com",),
    "direct-mongil": ("sgimage.netmarble.com",),
    "stove-czn": ("static-pubcomm.onstove.com",),
    "fgo-lepton": ("fate-go.us",),
    "arknights-yostar": ("webusstatic.yo-star.com",),
}


@dataclass(frozen=True)
class ArtCandidate:
    url: str
    orientation: str
    width: int
    height: int


def _dimension(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= MAX_DIMENSION else None


def _url_ok(url: Any, hosts: tuple[str, ...]) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port is not None:
        return False
    return (parsed.hostname or "").lower() in hosts


def parse_candidate(raw: Any, hosts: tuple[str, ...]) -> ArtCandidate | None:
    """One candidate, or ``None`` when anything about it is not acceptable."""
    if not isinstance(raw, dict) or not _url_ok(raw.get("url"), hosts):
        return None
    width, height = _dimension(raw.get("width")), _dimension(raw.get("height"))
    orientation = raw.get("orientation")
    if width is None or height is None or orientation not in ("wide", "portrait"):
        return None
    return ArtCandidate(raw["url"], orientation, width, height)


def valid_document(doc: Any) -> bool:
    """True when ``doc`` claims the schema version this build understands.

    Used as the validator of both index hooks, so a document from a newer schema is
    rejected before :func:`indexfetch.fetch_json` writes it over the last good copy.
    """
    # ``type(...) is int``: JSON ``true`` equals 1 in Python but is not schema 1.
    return isinstance(doc, dict) and type(doc.get("schema")) is int and doc["schema"] == SCHEMA


def parse(doc: Any) -> dict[str, list[ArtCandidate]]:
    """The accepted candidates per game (``{}`` for a document of the wrong shape)."""
    if not valid_document(doc):
        return {}
    games = doc.get("games")
    if not isinstance(games, dict):
        return {}
    out: dict[str, list[ArtCandidate]] = {}
    for kind, hosts in ALLOWED_HOSTS.items():
        entry = games.get(kind)
        covers = entry.get("cover") if isinstance(entry, dict) else None
        if not isinstance(covers, list):
            continue
        accepted = [c for raw in covers if (c := parse_candidate(raw, hosts)) is not None]
        if accepted:
            out[kind] = accepted[:MAX_CANDIDATES]
    return out


# ---- loading ----------------------------------------------------------------------------------
def _default_loader() -> Any:
    from . import indexfetch

    return indexfetch.fetch_json("catalog/art.json", validate=valid_document).data


_loader: Callable[[], Any] = _default_loader
_parsed: dict[str, list[ArtCandidate]] | None = None
_valid_until = 0.0
_state_lock = threading.Lock()
_load_lock = threading.Lock()
_thread: threading.Thread | None = None
#: Bumped whenever the state is cleared, so a load that was already running cannot store its
#: (older) answer afterwards.
_generation = 0


def set_loader(loader: Callable[[], Any] | None) -> None:
    """Replace how the document is fetched (tests); ``None`` restores the default."""
    global _loader
    _loader = loader or _default_loader


def _invalidate() -> None:
    """Forget the loaded document; a load that is still running will not store its answer."""
    global _parsed, _valid_until, _generation
    with _state_lock:
        _parsed, _valid_until = None, 0.0
        _generation += 1


def reset() -> None:
    """Forget the loaded document, any pending load and a replaced loader (tests)."""
    global _loader
    _invalidate()
    wait_for_load(2)
    _invalidate()
    _loader = _default_loader


def _fresh() -> dict[str, list[ArtCandidate]] | None:
    with _state_lock:
        if _parsed is not None and time.monotonic() < _valid_until:
            return _parsed
    return None


def _load_now() -> dict[str, list[ArtCandidate]]:
    global _parsed, _valid_until
    with _load_lock:
        again = _fresh()
        if again is not None:  # another thread loaded it while this one waited
            return again
        with _state_lock:
            generation = _generation
        try:
            parsed, ttl = parse(_loader()), TTL
        except Exception as exc:  # noqa: BLE001 - offline or broken index: the old sources stay
            log.debug("art index unavailable: %s", exc)
            # The copy on disk still answers during the outage; only the retry wait is reset.
            parsed, ttl = _saved_copy(), RETRY_AFTER_FAILURE
        with _state_lock:
            if generation == _generation:
                _parsed, _valid_until = parsed, time.monotonic() + ttl
        return parsed


def _saved_copy() -> dict[str, list[ArtCandidate]]:
    """What the index said the last time it was reachable (disk only, never the network)."""
    from . import indexfetch

    try:
        return parse(indexfetch.read_cached("catalog/art.json", validate=valid_document))
    except Exception as exc:  # noqa: BLE001 - an unreadable copy is just no copy
        log.debug("art index: saved copy unreadable: %s", exc)
        return {}


def _start_background() -> None:
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_load_now, name="nadare-art-index", daemon=True)
        _thread.start()


def wait_for_load(timeout: float = 5.0) -> None:
    """Wait for a background load to finish (tests)."""
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def lookup(kind: str, *, wait: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """The index art for ``kind`` as ``(url, allowed hosts)``, best first.

    With ``wait=False`` this never waits for the network: while the document is not loaded it
    answers from the copy saved on disk (if any) and starts a background refresh, so a cover that
    is already on disk is never held up by a slow or absent network. Use ``wait=True`` when
    there is nothing to show yet, or when the user asked for the art explicitly.
    """
    parsed = _fresh()
    if parsed is None:
        if wait:
            parsed = _load_now()
        else:
            # Answer at once from the copy saved on disk, and refresh it in the background: a
            # new process does not need the network to know the list it saw last time.
            parsed = _saved_copy()
            _start_background()
    hosts = ALLOWED_HOSTS.get(kind, ())
    return [(candidate.url, hosts) for candidate in parsed.get(kind, ())]


__all__ = [
    "ALLOWED_HOSTS",
    "ArtCandidate",
    "lookup",
    "parse",
    "reset",
    "set_loader",
    "valid_document",
    "wait_for_load",
]
