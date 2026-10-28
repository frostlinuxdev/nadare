# SPDX-License-Identifier: GPL-3.0-or-later
"""The source-checkout requirements include every runtime dependency."""

from pathlib import Path


def test_source_requirements_include_zstandard():
    requirements = Path(__file__).parents[1].joinpath("requirements.txt").read_text()
    assert "zstandard==" in requirements
