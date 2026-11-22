#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Warn when the cover art published in the index (``catalog/art.json``) has gone stale.

Many of the URLs carry a content hash and die when a publisher updates the art. For each game
this probes its candidates in order; a game with at least one working candidate is fine (a
dead first candidate is only noted), a game where none works is a problem, because clients
then fall back to the older art sources. A candidate the app itself would refuse (wrong host,
not https, bad size) is reported too.

Exit status: 0 = fine, 4 = a problem. Problems are printed as ``CANARY: ...`` lines, like the
other canaries, so the refresh job's heartbeat can go "down". As with the other canaries, "could
not check" is not "fine": a game none of whose candidates could be reached at all is reported as
blind, which also exits 4. A redirect counts as a failure, because the app does not follow one.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROBLEM = 4
TIMEOUT = 15
#: Anything smaller than this is not cover art (an error image or a tracking pixel).
MIN_BYTES = 2_000

#: What this run could not look at; :func:`main` reports each as a ``CANARY:`` line.
BLIND: list[str] = []

#: ``(HTTP status, content type, size in bytes or None when the server did not say)``.
Head = Callable[[str], tuple[int, str, int | None]]


def _blind(message: str) -> None:
    print(f"art index: {message}", file=sys.stderr)
    BLIND.append(message)


def _artindex():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from nadare.core import artindex

    return artindex


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the app's downloader does not, and it could leave the host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _answer(status: int, headers: Any) -> tuple[int, str, int | None]:
    content_type = (headers.get("Content-Type") if headers else "") or ""
    size: int | None = None
    content_range = headers.get("Content-Range") if headers else None
    if content_range and "/" in content_range:  # "bytes 0-0/12345" from a ranged GET
        total = content_range.rsplit("/", 1)[1].strip()
        size = int(total) if total.isdigit() else None
    if size is None:
        length = headers.get("Content-Length") if headers else None
        if length is not None and length.strip().isdigit():
            size = int(length)
    return status, content_type, size


def _probe(url: str, method: str) -> tuple[int, str, int | None]:
    headers = {"User-Agent": "nadare-art-canary"}
    if method == "GET":
        headers["Range"] = "bytes=0-0"
    request = urllib.request.Request(url, method=method, headers=headers)  # noqa: S310
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:  # noqa: S310
            return _answer(response.status, response.headers)
    except urllib.error.HTTPError as exc:
        return _answer(exc.code, exc.headers)


#: Generic types some CDNs (Netmarble, STOVE) serve real images with; the app sniffs the bytes.
_GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream")
#: The magic bytes the app's own check (``artwork._looks_like_image``) accepts, with their type.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _sniff(url: str) -> str:
    """The image type ``url``'s first bytes show, or ``""``."""
    request = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "nadare-art-canary", "Range": "bytes=0-15"}
    )
    with _OPENER.open(request, timeout=TIMEOUT) as response:  # noqa: S310
        head = response.read(16)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for magic, kind in _MAGIC if head.startswith(magic)), "")


def _head(url: str) -> tuple[int, str, int | None]:
    """What ``url`` answers, without following a redirect.

    Some CDNs refuse HEAD (403, 405, 501) but serve a ranged GET; that is tried next. An answer
    with a generic octet-stream type is judged by its first bytes, as the app judges it.
    """
    answer = _probe(url, "HEAD")
    if answer[0] in (403, 405, 501):
        answer = _probe(url, "GET")
    status, content_type, size = answer
    if status in (200, 206) and content_type.split(";")[0].strip().lower() in _GENERIC_TYPES:
        sniffed = _sniff(url)
        if sniffed:
            return status, sniffed, size
    return answer


def _works(answer: tuple[int, str, int | None]) -> bool:
    status, content_type, size = answer
    if status not in (200, 206) or not content_type.lower().startswith("image/"):
        return False
    return size is None or size >= MIN_BYTES  # None = the server did not say


def check(doc: Any, head: Head) -> list[str]:
    """Probe every game's cover and wide candidates and list the problems."""
    artindex = _artindex()
    games = doc.get("games") if isinstance(doc, dict) else None
    if not isinstance(games, dict) or not games:
        return ["art index: art.json lists no games, so the app has no index art"]
    problems: list[str] = []
    for art, parse in (("cover", artindex.parse), ("wide", artindex.parse_wide)):
        accepted = parse(doc)
        listed = False
        for kind, entry in sorted(games.items()):
            if kind not in artindex.ALLOWED_HOSTS:
                continue
            offered = entry.get(art) if isinstance(entry, dict) else None
            if art == "wide" and not isinstance(offered, list):
                continue  # wide art is optional; many games only have a cover
            count = len(offered) if isinstance(offered, list) else 0
            listed = listed or bool(count)
            if kind not in accepted:
                problems.append(
                    f"art index: every {art} candidate for {kind} is not acceptable to the app "
                    "(wrong host, not https, or bad size)"
                )
            elif count > len(accepted[kind]):
                print(
                    f"art index: {count - len(accepted[kind])} candidate(s) of {kind} {art} are "
                    "not acceptable to the app and are ignored",
                    file=sys.stderr,
                )
        if not accepted and (art == "cover" or listed):
            # A missing wide list is normal (the store tile and hero fall back to the cover);
            # a wide list whose every entry the app refuses is not.
            problems.append(f"art index: no {art} candidate in art.json is acceptable to the app")
        for kind in sorted(accepted):
            outcomes: list[bool] = []
            failure: Exception | None = None
            for candidate in accepted[kind]:
                try:
                    answer = head(candidate.url)
                except Exception as exc:  # noqa: BLE001 - one flaky host must not hide a fallback
                    failure = exc
                    outcomes.append(False)
                    continue
                outcomes.append(_works(answer))
                if outcomes[-1]:
                    break  # one working candidate is enough; the rest are not probed
            if any(outcomes):
                if not outcomes[0]:
                    print(
                        f"art index: the first {art} candidate of {kind} is dead", file=sys.stderr
                    )
            elif failure is not None:
                _blind(f"could not check the {art} art for {kind}: {failure}")
            else:
                problems.append(
                    f"art index: no working {art} for {kind} (all candidates fail)"
                )
    return problems


def load_document(index: Path) -> Any:
    path = index / "catalog" / "art.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path, help="nadare-index checkout")
    args = parser.parse_args(argv)

    BLIND.clear()
    try:
        doc = load_document(args.index)
    except (OSError, ValueError) as exc:
        print(f"CANARY: art index: art.json cannot be read ({exc})")
        return PROBLEM
    if doc is None:
        print("art index: no art document published, nothing to check")
        return 0
    problems = check(doc, _head)
    problems += [f"art index: blind, {note}" for note in BLIND]
    for line in problems:
        print(f"CANARY: {line}")
    if not problems:
        print("art index: ok")
    return PROBLEM if problems else 0


if __name__ == "__main__":
    sys.exit(main())
