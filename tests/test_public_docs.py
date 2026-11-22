# SPDX-License-Identifier: GPL-3.0-or-later
"""#1235: the public docs never carry anti-cheat or anti-VM evasion guidance again.

#1236/#1238 removed such text (hiding a VM from a game, reading secrets out of a binary,
narrating how a launcher is bypassed). This guard keeps it out of every Markdown file the
release carries. Ordinary words ("anti-cheat", "hypervisor" in VM detection) stay allowed:
only phrases that read as instructions are listed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Phrases that read as evasion guidance or bypass narration (case-insensitive).
FORBIDDEN = (
    r"kvm\s*=\s*off",
    r"\bsmbios\b",
    r"anti[- ]?vm",
    r"hide (the|a|your) (vm|virtual machine|hypervisor)",
    r"official launcher never",
    r"read from the binary",
    r"sign\s*=\s*md5",
    r"bypass(es|ing)? the anti[- ]?cheat",
)
PATTERN = re.compile("|".join(FORBIDDEN), re.IGNORECASE)

#: Maintainer files the release never carries (release/public-only.txt in the workspace).
NOT_PUBLIC = ("TESTING.md", "research/", "scripts/nikke_launcherless/", "docs/test-reports/")


def _markdown_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        out = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.md") if ".git" not in p.parts]
    return [ROOT / rel for rel in out if not rel.startswith(NOT_PUBLIC)]


def test_public_docs_carry_no_evasion_guidance():
    files = _markdown_files()
    assert files, "no Markdown files found"
    found = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PATTERN.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}")
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "line",
    [
        "Set kvm=off in the VM so the game does not notice.",
        "Hide the hypervisor from the game with smbios values.",
        "The key is read from the binary at start-up.",
        "This bypasses the anti-cheat.",
    ],
)
def test_the_guard_catches_the_kind_of_text_it_is_for(line):
    assert PATTERN.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "The anti-cheat is not touched.",
        "Offer a log capture (PROTON_LOG=1) summarized with tokens redacted.",
    ],
)
def test_ordinary_words_stay_allowed(line):
    assert not PATTERN.search(line)
