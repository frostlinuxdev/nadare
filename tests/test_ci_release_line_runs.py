# SPDX-License-Identifier: GPL-3.0-or-later
"""A merge into the release line (b29, b30, ...) gets a full CI run of its own (2026-11-27).

Pull requests run CI, but nothing ran on the release line after a merge, so two PRs that
pass alone and break together surfaced only at the release cut.
"""

import fnmatch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _push_branches(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True
    return list(on["push"]["branches"])


def test_a_push_to_a_release_line_runs_ci():
    patterns = _push_branches(ROOT / ".forgejo" / "workflows" / "ci.yml")
    for branch in ("b29", "b30", "b123"):
        assert any(fnmatch.fnmatch(branch, p) for p in patterns), branch
    # Feature branches stay on their pull_request run only.
    for branch in ("feat/x", "ci/apt-deb-cache", "rc/b27-rc5", "beta"):
        assert not any(fnmatch.fnmatch(branch, p) for p in patterns), branch


def test_the_picker_runs_everything_on_a_push():
    """A push has no base_ref, and the picker treats no base as "run the whole suite"."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci_pick_tests.py"), ""],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "lane=full" in out and "targets=tests" in out
