# SPDX-License-Identifier: GPL-3.0-or-later
"""Every tracked source file names its licence (#1227 F11): README.md and docs/AI.md say so."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = (".py", ".c", ".sh", ".spec", ".toml")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [name for name in out.splitlines() if name.endswith(SUFFIXES)]


def test_every_source_file_has_an_spdx_header():
    missing = []
    for name in _tracked():
        path = ROOT / name
        if not path.is_file():
            continue
        head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:5]
        if not any("SPDX-License-Identifier: GPL-3.0" in line for line in head):
            missing.append(name)
    assert not missing, f"add an SPDX header to: {missing}"


def test_license_is_the_verbatim_gpl3_text():
    """#1227 F9: the licence text says it may not be changed; ship GNU's file byte for byte."""
    import hashlib

    data = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(data).hexdigest() == (
        "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
    )
