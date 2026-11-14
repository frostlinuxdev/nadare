# SPDX-License-Identifier: GPL-3.0-or-later
"""The warn-only file size check (#1248)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_file_sizes", ROOT / "scripts" / "check_file_sizes.py"
)
cfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfs)


def test_a_new_big_file_warns():
    out = cfs.warnings({"src/a.py": 900, "src/b.py": 100}, {})
    assert len(out) == 1 and "src/a.py has 900 lines" in out[0] and out[0].startswith("::warning")


def test_a_grandfathered_file_warns_only_when_it_grows():
    assert cfs.warnings({"src/a.py": 900}, {"src/a.py": 950}) == []
    assert cfs.warnings({"src/a.py": 950}, {"src/a.py": 950}) == []
    out = cfs.warnings({"src/a.py": 951}, {"src/a.py": 950})
    assert len(out) == 1 and "grew from 950 to 951" in out[0]


def test_at_the_limit_is_fine():
    assert cfs.warnings({"src/a.py": cfs.LIMIT}, {}) == []
    assert cfs.warnings({"src/a.py": cfs.LIMIT + 1}, {}) != []


def test_it_never_fails_ci(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "big.py").write_text("x = 1\n" * (cfs.LIMIT + 5))
    assert cfs.main(["--root", str(tmp_path)]) == 0


def test_the_baseline_parses_and_ignores_comments(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("# comment\n900 src/a.py\n\nbad line\n")
    assert cfs.read_baseline(path) == {"src/a.py": 900}


def test_the_repo_baseline_lists_only_files_over_the_limit():
    baseline = cfs.read_baseline(ROOT / cfs.BASELINE)
    assert baseline and all(count > cfs.LIMIT for count in baseline.values())
