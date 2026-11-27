#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail a PR that adds text the public tree must not carry: the release cut runs the same
checks, but finding them there costs a merge and a cut (2026-11-27).

    check_public_text.py      (reads PRIVATE_NAMES and PUBLIC_ONLY from the environment)

PRIVATE_NAMES: one name per line (# comments), matched case-insensitively in every tracked file,
tests included. The list is private, so it comes from a CI secret; without it (the public CI)
the scan is skipped. PUBLIC_ONLY: paths kept out of the public tree, left out of both checks.
Hits print as path:line only, so the log never repeats a private word.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Content the public tree must never say (outside tests). Written so this file does not match.
CONTENT_FAIL = r"maintainer'?s account|reverse[- ]?engineer|decompi[l]"


def _lines(value: str) -> list[str]:
    out = []
    for line in value.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _grep(cwd: Path, args: list[str], pathspec: list[str]) -> list[str]:
    proc = subprocess.run(
        ["git", "grep", "-n", "-I", *args, "--", *pathspec],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"git grep failed: {proc.stderr.strip()}")
    # "path:line:text" -> "path:line"
    return [":".join(hit.split(":", 2)[:2]) for hit in proc.stdout.splitlines()]


def main(cwd: Path = Path("."), env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    names = _lines(env.get("PRIVATE_NAMES", ""))
    if not names:
        print("PUBLIC-TEXT: skipped (no PRIVATE_NAMES list in this CI)")
        return 0
    keep_out = [f":!{p}" for p in _lines(env.get("PUBLIC_ONLY", ""))]
    name_hits = _grep(cwd, ["-i", "-F", *[a for n in names for a in ("-e", n)]], [".", *keep_out])
    content_hits = _grep(cwd, ["-i", "-E", "-e", CONTENT_FAIL], [".", ":!tests", *keep_out])
    for hit in name_hits:
        print(f"NAME-FAIL {hit}")
    for hit in content_hits:
        print(f"CONTENT-FAIL {hit}")
    if name_hits or content_hits:
        print(
            f"PUBLIC-TEXT: {len(name_hits)} private names, {len(content_hits)} must-not-say hits: "
            "use neutral names, or move the text out of the public paths"
        )
        return 1
    print(f"PUBLIC-TEXT: clean ({len(names)} names checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
