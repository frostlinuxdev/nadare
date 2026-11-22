# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1549: the licence is declared the PEP 639 way (an SPDX expression plus the files)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_licence_is_an_spdx_expression() -> None:
    licence = _pyproject()["project"]["license"]
    # The old table form ({text = ...}) is deprecated by PEP 639. The wheel also
    # ships the Inter font (assets/fonts), which is OFL-1.1.
    assert licence == "GPL-3.0-only AND OFL-1.1"


def test_licence_files_are_declared_and_exist() -> None:
    files = _pyproject()["project"]["license-files"]
    assert "LICENSE" in files
    assert "src/nadare/assets/fonts/OFL.txt" in files
    for pattern in files:
        assert list(REPO.glob(pattern)), f"license-files pattern matches nothing: {pattern}"


def test_no_licence_classifier_next_to_the_expression() -> None:
    # setuptools refuses a "License ::" classifier together with an SPDX expression.
    classifiers = _pyproject()["project"].get("classifiers", [])
    assert not [c for c in classifiers if c.startswith("License ::")]


def test_build_floor_understands_pep639() -> None:
    # PEP 639 support landed in setuptools 77.
    requires = _pyproject()["build-system"]["requires"]
    floors = [re.fullmatch(r"setuptools>=(\d+)(\.\d+)*", r) for r in requires]
    floors = [int(m.group(1)) for m in floors if m]
    assert floors and floors[0] >= 77, requires
