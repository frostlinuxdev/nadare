# SPDX-License-Identifier: GPL-3.0-or-later
"""#812: every parser of untrusted data survives a short, fixed-seed mutation run.

Each target runs in its own process (``scripts/fuzz.py``) under an address-space cap, so a
blow-up cannot take the test run with it. ``NADARE_FUZZ_ITERATIONS`` makes it longer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fuzz.py"
ITERATIONS = int(os.environ.get("NADARE_FUZZ_ITERATIONS", "300"))


def _targets() -> list[str]:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--list"], capture_output=True, text=True, check=True
    )
    return out.stdout.split()


TARGETS = _targets()


def test_every_parser_family_is_covered():
    for needed in (
        "sevenz",
        "sophon",
        "krdiff",
        "yostar-manifest",
        "shortcuts-vdf",
        "steam-config-vdf",
        "source-wuwa",
        "notices",
        "archivepins",
    ):
        assert needed in TARGETS


@pytest.mark.parametrize("target", TARGETS)
def test_no_finding(target, tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--target",
            target,
            "--iterations",
            str(ITERATIONS),
            "--seed",
            "812",
            "--save",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode in (0, 1), proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert report["findings"] == {}, f"{target}: {report['findings']} (inputs in {tmp_path})"


def test_the_fuzzer_reports_a_crash(tmp_path):
    """The harness itself: an unexpected exception is a finding, exit code 1, input saved."""
    scripts, save = str(ROOT / "scripts"), str(tmp_path)
    code = (
        f"import sys; sys.path.insert(0, {scripts!r}); import fuzz, pathlib\n"
        "t = fuzz.Target(lambda d: 1 / 0, (ValueError,), lambda rng: b'x')\n"
        f"f = fuzz.fuzz('boom', t, 3, 1, 1.0, pathlib.Path({save!r}))\n"
        "print(f)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "ZeroDivisionError" in out.stdout
    assert list(tmp_path.glob("boom-*.bin"))
