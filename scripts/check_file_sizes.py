#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Warn (never fail) when a Python file grows past the size limit (#1248).

    check_file_sizes.py [--root DIR] [--write-baseline]

Files under src/ and tests/ over LIMIT lines are listed in scripts/file_size_baseline.txt with their
size when the limit came in (grandfathered). A NEW file over the limit, or a listed file that grew,
gets a CI warning (``::warning file=...``). Splitting big files is tracked in #1248; the baseline
only ever shrinks: --write-baseline rewrites it from the current tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIMIT = 800
BASELINE = Path("scripts") / "file_size_baseline.txt"


def sizes(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for folder in ("src", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            with path.open("rb") as handle:
                out[path.relative_to(root).as_posix()] = sum(1 for _ in handle)
    return out


def read_baseline(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        count, _, name = line.partition(" ")
        if count.isdigit() and name:
            out[name.strip()] = int(count)
    return out


def warnings(current: dict[str, int], baseline: dict[str, int], limit: int = LIMIT) -> list[str]:
    out = []
    for name, lines in sorted(current.items()):
        if lines <= limit:
            continue
        known = baseline.get(name)
        if known is None:
            out.append(f"::warning file={name}::{name} has {lines} lines (limit {limit}); split it "
                       "(see #1248) or add it to scripts/file_size_baseline.txt with a reason")
        elif lines > known:
            out.append(f"::warning file={name}::{name} grew from {known} to {lines} lines "
                       f"(over the {limit}-line limit; see #1248)")
    return out


def main(argv: list[str]) -> int:
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path(".")
    current = sizes(root)
    if "--write-baseline" in argv:
        big = {n: c for n, c in current.items() if c > LIMIT}
        (root / BASELINE).write_text(
            "# Files over the size limit when it came in (#1248): lines, path. Only shrinks.\n"
            + "".join(f"{c} {n}\n" for n, c in sorted(big.items())),
            encoding="utf-8",
        )
        print(f"baseline: {len(big)} files over {LIMIT} lines")
        return 0
    found = warnings(current, read_baseline(root / BASELINE))
    for line in found:
        print(line)
    print(f"file sizes: {len(found)} warning(s); limit {LIMIT} lines")
    return 0  # warn only (#1248)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
