#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Warn when a pinned NTE or Endfield installer has been replaced or moved.

Nadare downloads the Neverness to Everness installer from a fixed URL and pins
its SHA-256 and size, and it takes the Endfield launcher installer from an API
response whose host must be allowed. This asks the publisher for the current
installer and reports a missing NTE file, a changed size, or an Endfield host
that Nadare would refuse, so a stale pin can turn the refresh job's heartbeat
"down" before users hit it.

Once a day it also downloads the Endfield launcher installer and checks that it still yields
``Games.exe`` (what Nadare needs to start the game), using the same extraction code the app uses.
That needs 7-Zip on the machine; without it the check is skipped with a note.

Exit status: 0 = fine (or nothing could be checked), 4 = a pin went stale.
Problems are printed as ``CANARY: ...`` lines.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROBLEM = 4
#: Seconds to wait for one installer check.
TIMEOUT = 15
_USER_AGENT = "nadare-installer-canary"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nadare.core import download as _dl  # noqa: E402
from nadare.core import endfield_shim, nte_launcher, sevenzip  # noqa: E402

#: The installer is ~124 MiB: look inside it at most once a day, and retry a failure hourly.
DEEP_INTERVAL = 24 * 3600
RETRY_INTERVAL = 3600


def check_nte(head: Callable[[str], tuple[int, int | None]]) -> list[str]:
    """Check the pinned NTE installer and list the problems.

    ``head`` returns ``(status, content_length)``; missing lengths, 5xx answers,
    timeouts and network errors are only noted, so a flaky CDN never raises a
    false alarm.
    """
    problems: list[str] = []
    try:
        status, length = head(nte_launcher.INSTALLER_URL)
    except Exception as exc:  # noqa: BLE001 - a flaky CDN must not stop the run
        print(f"installer pins: could not check the NTE installer: {exc}", file=sys.stderr)
        return problems
    if status in (404, 410):
        problems.append(
            f"NTE installer URL is gone (HTTP {status}); find the new installer "
            "and re-pin INSTALLER_URL, INSTALLER_SHA256 and INSTALLER_SIZE"
        )
    elif status == 200:
        if length is None:
            print(
                "installer pins: the NTE installer returned no Content-Length, ignored",
                file=sys.stderr,
            )
        elif length != nte_launcher.INSTALLER_SIZE:
            problems.append(
                f"NTE installer changed size (now {length}, "
                f"pinned {nte_launcher.INSTALLER_SIZE}); "
                "re-pin INSTALLER_SHA256 and INSTALLER_SIZE"
            )
    elif status >= 500:
        print(f"installer pins: NTE installer returned HTTP {status}, ignored", file=sys.stderr)
    else:
        print(f"installer pins: NTE installer returned HTTP {status}, ignored", file=sys.stderr)
    return problems


def check_endfield(get_json: Callable[[str], object]) -> list[str]:
    """Check the Endfield launcher installer host and list the problems.

    ``get_json`` returns the decoded API object; network and JSON errors are only
    noted, so a flaky API never raises a false alarm.
    """
    problems: list[str] = []
    try:
        data = get_json(endfield_shim.LAUNCHER_API)
    except Exception as exc:  # noqa: BLE001 - a flaky API must not stop the run
        print(f"installer pins: could not check the Endfield installer: {exc}", file=sys.stderr)
        return problems
    url = data.get("exe_url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        problems.append("Endfield launcher API returned no exe_url")
    elif not endfield_shim.installer_host_allowed(url):
        host = urllib.parse.urlparse(url).hostname or url
        problems.append(
            f"Endfield launcher installer now comes from {host}, which Nadare "
            "does not allow; update ALLOWED_INSTALLER_HOSTS"
        )
    return problems


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, status: str, problems: list[str], now: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(
            json.dumps({"checked_at": now, "status": status, "problems": problems}),
            encoding="utf-8",
        )
        os.replace(part, path)
    except OSError as exc:
        print(f"installer pins: could not save the canary state: {exc}", file=sys.stderr)


def check_endfield_games_exe(
    get_json: Callable[[str], object],
    download: Callable[[str, Path], Any],
    extract: Callable[[Path, Path], Any],
    *,
    state_path: Path,
    now: float,
    find_7zip: Callable[[], str | None],
) -> list[str]:
    """Check that the Endfield launcher installer still yields ``Games.exe``.

    Daily when the last answer was fine; hourly after a failure (a known problem is repeated
    meanwhile so the heartbeat stays down). A network failure or a missing 7-Zip is only a note.
    """
    state = _read_state(state_path)
    age = now - state["checked_at"] if isinstance(state.get("checked_at"), (int, float)) else None
    status = state.get("status")
    if age is not None and 0 <= age:
        if status == "ok" and age < DEEP_INTERVAL:
            return []
        if status == "unreachable" and age < RETRY_INTERVAL:
            return []
        if status == "problem" and age < RETRY_INTERVAL:
            stored = state.get("problems")
            return [str(x) for x in stored] if isinstance(stored, list) else []
    if find_7zip() is None:
        print(
            "installer pins: 7-Zip is not installed here, so the Endfield Games.exe check "
            "is skipped",
            file=sys.stderr,
        )
        return []
    try:
        data = get_json(endfield_shim.LAUNCHER_API)
    except Exception as exc:  # noqa: BLE001 - check_endfield reports API trouble
        print(
            f"installer pins: could not ask the Endfield API for Games.exe: {exc}",
            file=sys.stderr,
        )
        return []
    url = data.get("exe_url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not endfield_shim.installer_host_allowed(url):
        return []  # check_endfield already reports a missing or foreign installer URL
    with tempfile.TemporaryDirectory(prefix="endfield-canary-") as tmp:
        installer = Path(tmp) / "launcher.exe"
        try:
            download(url, installer)
        except Exception as exc:  # noqa: BLE001 - a flaky CDN must not raise a false alarm
            print(
                f"installer pins: could not download the Endfield installer to look for "
                f"Games.exe: {exc}",
                file=sys.stderr,
            )
            _write_state(state_path, "unreachable", [], now)
            return []
        try:
            extract(installer, Path(tmp) / "Games.exe")
        except endfield_shim.ShimError as exc:
            problems = [f"Endfield launcher installer no longer yields Games.exe: {exc}"]
            _write_state(state_path, "problem", problems, now)
            return problems
    _write_state(state_path, "ok", [], now)
    return []


def _download(url: str, dest: Path) -> Any:
    return _dl.download(url, dest, redirect_allowed=endfield_shim.installer_host_allowed)


def decode_body(raw: bytes) -> str:
    """Response text; the launcher API may come back gzip-compressed."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
    return raw.decode("utf-8", "replace")


def _content_length(value: str | None) -> int | None:
    """The Content-Length header as an int, or ``None`` when absent or unusable."""
    if value is None or not value.strip().isdigit():
        return None
    return int(value)


def _head(url: str) -> tuple[int, int | None]:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, _content_length(response.headers.get("Content-Length"))
    except urllib.error.HTTPError as exc:
        return exc.code, _content_length(exc.headers.get("Content-Length"))


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(decode_body(response.read(1_000_000)))
    except urllib.error.HTTPError as exc:
        return json.loads(decode_body(exc.read(65536)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(
            os.environ.get("NADARE_CANARY_STATE")
            or Path.home() / ".cache" / "nadare-installer-canary.json"
        ),
        help="where the daily Games.exe check remembers its last answer",
    )
    args = parser.parse_args(argv)
    problems = check_nte(_head) + check_endfield(_get_json)
    problems += check_endfield_games_exe(
        _get_json,
        _download,
        endfield_shim.extract_games_exe,
        state_path=args.state,
        now=time.time(),
        find_7zip=sevenzip.find,
    )
    for line in problems:
        print(f"CANARY: {line}")
    if not problems:
        print("installer pins: ok")
    return PROBLEM if problems else 0


if __name__ == "__main__":
    sys.exit(main())
