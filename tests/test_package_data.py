# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep pyproject's package-data patterns in step with the packaged files."""

from __future__ import annotations

import glob
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "nadare"


def _patterns() -> list[str]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["setuptools"]["package-data"]["nadare"]


def _matches(pattern: str) -> set[Path]:
    found = glob.glob(pattern, root_dir=PACKAGE, recursive=True)
    return {Path(path) for path in found if (PACKAGE / path).is_file()}


def test_every_asset_file_is_matched_by_package_data() -> None:
    packaged: set[Path] = set()
    for pattern in _patterns():
        packaged.update(_matches(pattern))

    assets = {
        path.relative_to(PACKAGE).as_posix()
        for path in (PACKAGE / "assets").rglob("*")
        if path.is_file()
    }
    missing = sorted(assets - {path.as_posix() for path in packaged})

    assert not missing, "pyproject.toml package-data omits: " + ", ".join(missing)


def test_package_data_patterns_match_real_files() -> None:
    dead = [pattern for pattern in _patterns() if not _matches(pattern)]

    assert not dead, "pyproject.toml package-data patterns match nothing: " + ", ".join(dead)
