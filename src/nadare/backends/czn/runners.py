#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 czn-linux contributors
"""Command-line front end for ``nadare.core.runners`` (the Proton runner manager).

  list                      the runners Nadare can see (its own, Steam's, system)
  install <id>              the tested release of dwproton | cachyos | ge
  install <id> --latest     the newest upstream release instead (untested)

The backend installs runners in-process through ``core.runners``; this entry
point only exists for running that code by hand. ``--json`` prints one JSON
object per line. Exit codes: 0 ok, 1 usage or install error, 4 network error,
9 checksum mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys

from nadare.core import download, runners
from nadare.core.events import Event


def _emit(event: Event, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"event": event.kind, "message": event.message, **event.data}))
    elif event.message:
        print("[*] " + event.message, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("list", "install"))
    ap.add_argument("runner", nargs="?", choices=tuple(runners.RUNNERS))
    ap.add_argument("--latest", action="store_true", help="newest upstream release (untested)")
    ap.add_argument("--json", action="store_true", help="JSON lines instead of text")
    a = ap.parse_args(argv)
    if a.action == "list":
        for row in runners.list_runners():
            print(json.dumps(row) if a.json else f"{row['name']}  {row['path']}")
        return 0
    if a.runner is None:
        ap.error("install needs a runner: " + ", ".join(runners.RUNNERS))
    try:
        dest = runners.install(a.runner, latest=a.latest, emit=lambda e: _emit(e, a.json))
    except download.IntegrityError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 9
    except (download.DownloadError, OSError) as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 4
    except runners.RunnerError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
