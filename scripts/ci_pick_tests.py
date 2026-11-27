#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pick the tests a pull request needs.

    ci_pick_tests.py BASE_REF      (prints lane=docs|full and targets=... for $GITHUB_OUTPUT)

A PR that changes only docs (Markdown outside src/, docs/, LICENSE) runs the test files that
read docs; anything else, and any doubt (no base, git failing, an empty diff), runs the whole
suite. About one merge in five was docs-only (2026-11-27), and each took a full run.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FULL = ("full", ["tests"])
# A test that names a docs path or a Markdown file is one a docs change can break.
READS_DOCS = re.compile(r"docs/|\.md\b|LICENSE")


def is_doc(path: str) -> bool:
    if path.startswith("src/"):
        return False  # packaged with the app: a code change
    return path.startswith("docs/") or path.endswith(".md") or path == "LICENSE"


def pick(changed: list[str], tests: dict[str, str]) -> tuple[str, list[str]]:
    if not changed or not all(is_doc(p) for p in changed):
        return FULL
    targets = sorted(p for p, text in tests.items() if READS_DOCS.search(text))
    return ("docs", targets) if targets else FULL


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=120
    ).stdout


def _changed(base: str, cwd: Path, fetch: bool) -> list[str]:
    if fetch:
        # The checkout is shallow: fetch enough of both sides to find the merge base.
        _git(cwd, "fetch", "-q", "--no-tags", "--depth=200", "origin", base)
        _git(cwd, "fetch", "-q", "--no-tags", "--deepen=200", "origin")
    return _git(cwd, "diff", "--name-only", f"origin/{base}...HEAD").split()


def main(argv: list[str], cwd: Path = Path("."), fetch: bool = True) -> int:
    base = argv[0] if argv else ""
    lane, targets = FULL
    if base:
        try:
            changed = _changed(base, cwd, fetch)
            tests = {
                str(p.relative_to(cwd)): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted((cwd / "tests").glob("test_*.py"))
            }
            lane, targets = pick(changed, tests)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"ci_pick_tests: running everything ({exc.__class__.__name__})", file=sys.stderr)
    print(f"lane={lane}")
    print("targets=" + " ".join(targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
