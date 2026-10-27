#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Warn before a new Proton runner outruns the Steam Runtimes the pinned umu knows.

A runner's ``toolmanifest.vdf`` names the Steam Runtime it needs
(``require_tool_appid``). If umu does not know that id it fails with "Failed to
match ... with a container runtime", which is what an old system umu did on
Bazzite. This checks the latest release of every runner family and reports any
id missing from ``nadare.core.umu.SUPPORTED_RUNTIME_APPIDS``.

A runner tarball is only downloaded (streamed, stopping at the manifest) when its
tag is new; the answer is kept in a small state file, so an hourly run costs one
API call per family.

Exit status: 0 = fine (or nothing could be checked), 4 = a runner needs an
unsupported runtime. Problems are printed as ``CANARY: ...`` lines.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

PROBLEM = 4
#: Stop reading a tarball after this many compressed bytes without a manifest.
_MAX_BYTES = 700 * 1024 * 1024
_APPID_RE = re.compile(r'"require_tool_appid"\s+"(\d+)"')


def parse_required_appid(text: str) -> int | None:
    """The runtime app id a ``toolmanifest.vdf`` requires, or ``None``."""
    match = _APPID_RE.search(text or "")
    return int(match.group(1)) if match else None


def read_manifest(fileobj: IO[bytes]) -> str | None:
    """Stream a compressed tarball (xz or gzip) and return the top-level ``toolmanifest.vdf`` text.

    Stops as soon as the manifest is found; nested copies are ignored.
    """
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            parts = [p for p in member.name.split("/") if p and p != "."]
            if member.isfile() and len(parts) <= 2 and parts[-1] == "toolmanifest.vdf":
                handle = tar.extractfile(member)
                if handle is None:
                    return None
                return handle.read(65536).decode("utf-8", "replace")
    return None


#: What this run could not look at; :func:`main` reports each as a ``CANARY:`` line, because
#: "could not check" is not "fine".
BLIND: list[str] = []


def _blind(message: str) -> None:
    print(f"canary: {message}", file=sys.stderr)
    BLIND.append(message)


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """The saved ``{family: {tag, appid}}`` answers; anything unreadable is empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data else {}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def check(
    state: Mapping[str, Mapping[str, Any]],
    releases: Mapping[str, str],
    fetch_appid: Callable[[str, str], int | None],
    supported: Mapping[int, str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Update ``state`` for the latest ``releases`` and list the problems.

    ``fetch_appid`` is only called for a tag not already in ``state``; when it
    fails the previous answer is kept, so a flaky network never raises a false alarm.
    """
    from nadare.core import umu

    new_state = {k: dict(v) for k, v in state.items()}
    for family, tag in releases.items():
        known = new_state.get(family)
        if known is not None and known.get("tag") == tag:
            continue
        try:
            appid = fetch_appid(family, tag)
        except Exception as exc:  # noqa: BLE001 - one bad family must not stop the rest
            _blind(f"could not read {family} {tag}: {exc}")
            continue
        new_state[family] = {"tag": tag, "appid": appid}
    problems: list[str] = []
    for family, tag in releases.items():
        entry = new_state.get(family)
        if not entry or entry.get("tag") != tag:
            continue
        appid = entry.get("appid")
        if appid is not None and appid not in supported:
            problems.append(
                f"{family} {tag} needs Steam Runtime app {appid}, which umu "
                f"{umu.UMU_VERSION} (Nadare's pinned copy) does not know; bump UMU_VERSION "
                "and SUPPORTED_RUNTIME_APPIDS before recommending it"
            )
    return new_state, problems


def _fetch_appid(family: str, tag: str) -> int | None:
    from nadare.core import runners

    spec = runners.RUNNERS[family]
    rel = runners._release(spec, tag)  # noqa: SLF001
    url = str(rel["tar"].get("browser_download_url") or "")
    if not runners._github_asset_host_allowed(url):  # noqa: SLF001
        raise ValueError(f"untrusted download host: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "nadare-runtime-canary"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - host checked
        limited = _Limited(response, _MAX_BYTES)
        return parse_required_appid(read_manifest(limited) or "")


class _Limited:
    """File-like wrapper that refuses to read more than ``limit`` bytes."""

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        self._raw, self._left = raw, limit

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._left -= len(data)
        if self._left < 0:
            raise ValueError("runner tarball larger than expected without a manifest")
        return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / "nadare-index-refresh" / "runtime-canary.json",
    )
    args = parser.parse_args(argv)

    from nadare.core import runners, umu

    BLIND.clear()
    releases: dict[str, str] = {}
    for family, spec in runners.RUNNERS.items():
        try:
            releases[family] = str(runners._release(spec, None)["tag"])  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            _blind(f"cannot look up {family}: {exc}")
    state, problems = check(
        load_state(args.state), releases, _fetch_appid, umu.SUPPORTED_RUNTIME_APPIDS
    )
    save_state(args.state, state)
    problems += [f"runtime canary blind, {note}" for note in BLIND]
    for line in problems:
        print(f"CANARY: {line}")
    if not problems:
        print(f"canary: ok ({', '.join(f'{k} {v}' for k, v in releases.items())})")
    return PROBLEM if problems else 0


if __name__ == "__main__":
    sys.exit(main())
