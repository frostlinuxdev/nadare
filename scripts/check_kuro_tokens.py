#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Warn when the Kuro launcher token published in the index is no longer accepted.

The maintainer keeps ``<index>/catalog/kuro-tokens.json`` with the current
``<prefix>_<token>`` values for Wuthering Waves and Punishing: Gray Raven. This
asks each game's launcher index with the published token and reports a refusal
or an error page, so a rotated token can turn the refresh job's heartbeat "down"
before users hit it.

Exit status: 0 = fine (or nothing could be checked), 4 = a published token is
malformed, refused or answered with an unexpected page. Problems are printed as
``CANARY: ...`` lines.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PROBLEM = 4

#: What this run could not look at; :func:`main` reports each as a ``CANARY:`` line, because
#: "could not check" is not "fine".
BLIND: list[str] = []


def _blind(message: str) -> None:
    print(f"kuro tokens: {message}", file=sys.stderr)
    BLIND.append(message)
#: Seconds to wait for one launcher index.
TIMEOUT = 15
#: game -> (launcher group, launcher prefix)
GAMES = {
    "wuwa": ("G153", "50004"),
    "pgr": ("G143", "50015"),
}
_URL = "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/{group}/{token}/index.json"
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,64}")


def load_published(index: Path) -> dict[str, Any] | None:
    """The published ``tokens`` table, or ``None`` when the file is absent or unreadable."""
    path = index / "catalog" / "kuro-tokens.json"
    try:
        if (index / ".git").exists():
            # Check what clients can download (the committed file), not a local edit.
            committed = subprocess.run(
                ["git", "-C", str(index), "show", "HEAD:catalog/kuro-tokens.json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            if committed.returncode != 0:
                return None
            try:
                local = path.read_text(encoding="utf-8")
            except OSError:
                local = None
            if local != committed.stdout:
                _blind(
                    "catalog/kuro-tokens.json has uncommitted changes or is missing locally; "
                    "checking the committed one"
                )
            text = committed.stdout
        else:
            text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def builtin_tokens() -> dict[str, str]:
    """The tokens compiled into Nadare, as ``<prefix>_<token>`` values."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from nadare.core.sources import pgr, wuwa

    return {
        "wuwa": f"{GAMES['wuwa'][1]}_{wuwa.BUILTIN_TOKEN}",
        "pgr": f"{GAMES['pgr'][1]}_{pgr.TOKEN}",
    }


def _well_formed(value: Any, prefix: str) -> bool:
    """True for a ``<prefix>_<token>`` value with a token of the expected shape."""
    if not isinstance(value, str):
        return False
    head, _, token = value.partition("_")
    return head == prefix and _TOKEN_RE.fullmatch(token) is not None


def _has_default(body: str) -> bool:
    """True when a 200 response is the launcher index (it always has a ``default`` key)."""
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and "default" in data


def check(
    published: Mapping[str, Any],
    fetch: Callable[[str], tuple[int, str]],
) -> list[str]:
    """Check every published token and list the problems.

    ``fetch`` returns ``(status, body)``; network errors and 5xx answers are only
    noted, so a flaky CDN never raises a false alarm.
    """
    problems: list[str] = []
    for game, (group, prefix) in GAMES.items():
        value = published.get(game)
        if value is None:
            continue
        if not _well_formed(value, prefix):
            problems.append(f"Kuro token for {game} is malformed in the index")
            continue
        url = _URL.format(group=group, token=value)
        try:
            status, body = fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad game must not stop the rest
            _blind(f"could not check {game}: {exc}")
            continue
        if 400 <= status < 500:
            problems.append(
                f"Kuro token for {game} was refused (HTTP {status}); "
                "update catalog/kuro-tokens.json in the index"
            )
        elif status >= 500:
            _blind(f"{game} returned HTTP {status}")
        elif status == 200 and not _has_default(body):
            problems.append(f"Kuro token for {game} returned an unexpected page")
    return problems


def decode_body(raw: bytes) -> str:
    """Response text; Kuro's launcher index comes back gzip-compressed."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
    return raw.decode("utf-8", "replace")


def _fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "nadare-kuro-canary"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, decode_body(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        return exc.code, decode_body(exc.read(65536))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path, help="nadare-index checkout")
    args = parser.parse_args(argv)

    BLIND.clear()
    tokens = load_published(args.index)
    if tokens is None:
        tokens = builtin_tokens()
        print("kuro tokens: no published file, checking the built-in tokens")
    problems = check(tokens, _fetch)
    problems += [f"kuro tokens: blind, {note}" for note in BLIND]
    for line in problems:
        print(f"CANARY: {line}")
    if not problems:
        print("kuro tokens: ok")
    return PROBLEM if problems else 0


if __name__ == "__main__":
    sys.exit(main())
