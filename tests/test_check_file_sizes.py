# SPDX-License-Identifier: GPL-3.0-or-later
"""The file size check (#1248): warn-only in normal mode; --write-baseline only ever lowers."""

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


def test_the_repo_baseline_does_not_list_tests():
    baseline = cfs.read_baseline(ROOT / cfs.BASELINE)
    assert baseline and all(name.startswith("src/") for name in baseline)


def _put(root: Path, name: str, lines: int) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines)


def _put_baseline(root: Path, entries: dict[str, int]) -> None:
    path = root / cfs.BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{count} {name}\n" for name, count in entries.items()))


def test_only_src_files_are_measured(tmp_path):
    _put(tmp_path, "src/a.py", 3)
    _put(tmp_path, "tests/b.py", 3)
    _put(tmp_path, "scripts/c.py", 3)
    assert set(cfs.sizes(tmp_path)) == {"src/a.py"}


def test_a_big_tests_file_is_not_warned_about(tmp_path, capsys):
    _put(tmp_path, "tests/big.py", cfs.LIMIT + 50)
    assert cfs.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "tests/big.py" not in out and "0 warning(s)" in out


def test_growth_is_still_a_warning_only(tmp_path, capsys):
    _put(tmp_path, "src/a.py", cfs.LIMIT + 50)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 10})
    assert cfs.main(["--root", str(tmp_path)]) == 0
    assert "grew from" in capsys.readouterr().out


def test_write_baseline_never_raises_an_entry(tmp_path):
    _put(tmp_path, "src/a.py", cfs.LIMIT + 150)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 100})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    assert cfs.read_baseline(tmp_path / cfs.BASELINE) == {"src/a.py": cfs.LIMIT + 100}


def test_write_baseline_lowers_an_entry_that_shrank(tmp_path):
    _put(tmp_path, "src/a.py", cfs.LIMIT + 50)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 200})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    assert cfs.read_baseline(tmp_path / cfs.BASELINE) == {"src/a.py": cfs.LIMIT + 50}


def test_write_baseline_drops_a_file_now_under_the_limit(tmp_path):
    _put(tmp_path, "src/a.py", cfs.LIMIT)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 100})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    assert cfs.read_baseline(tmp_path / cfs.BASELINE) == {}


def test_write_baseline_drops_tests_entries(tmp_path):
    _put(tmp_path, "tests/big.py", cfs.LIMIT + 100)
    _put_baseline(tmp_path, {"tests/big.py": cfs.LIMIT + 100})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    assert cfs.read_baseline(tmp_path / cfs.BASELINE) == {}


def test_write_baseline_refuses_a_new_file_over_the_limit(tmp_path, capsys):
    _put(tmp_path, "src/a.py", cfs.LIMIT + 100)
    _put(tmp_path, "src/new.py", cfs.LIMIT + 200)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 150})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) != 0
    written = cfs.read_baseline(tmp_path / cfs.BASELINE)
    assert written == {"src/a.py": cfs.LIMIT + 100}
    out = capsys.readouterr().out
    assert "src/new.py" in out and "not added" in out


def test_write_baseline_header_says_src_only(tmp_path):
    _put(tmp_path, "src/a.py", cfs.LIMIT + 100)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 100})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    header = (tmp_path / cfs.BASELINE).read_text(encoding="utf-8").splitlines()[0]
    assert "src/" in header and "tests/" not in header


def test_write_baseline_reports_every_new_file_with_spaces(tmp_path, capsys):
    root = tmp_path / "project with spaces"
    names = ("src/first big.py", "src/second big.py")
    for name in names:
        _put(root, name, cfs.LIMIT + 1)
    _put_baseline(root, {})

    assert cfs.main(["--root", str(root), "--write-baseline"]) == 1
    assert cfs.read_baseline(root / cfs.BASELINE) == {}
    out = capsys.readouterr().out
    for name in names:
        assert f"{name} has {cfs.LIMIT + 1} lines" in out
    assert "2 new file(s) not added" in out


def test_write_baseline_ignores_a_small_file_it_does_not_list(tmp_path):
    """Neon's mutant: a small unlisted src/ file is not new debt (order of the two checks)."""
    _put(tmp_path, "src/small.py", cfs.LIMIT)
    _put(tmp_path, "src/a.py", cfs.LIMIT + 10)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 10})
    assert cfs.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    assert cfs.read_baseline(tmp_path / cfs.BASELINE) == {"src/a.py": cfs.LIMIT + 10}


def test_one_line_of_growth_already_warns(tmp_path, capsys):
    """No growth threshold: a single added line is reported."""
    _put(tmp_path, "src/a.py", cfs.LIMIT + 11)
    _put_baseline(tmp_path, {"src/a.py": cfs.LIMIT + 10})
    assert cfs.main(["--root", str(tmp_path)]) == 0
    assert f"grew from {cfs.LIMIT + 10} to {cfs.LIMIT + 11}" in capsys.readouterr().out


def test_the_messages_do_not_ask_for_a_reason_in_the_baseline_file(tmp_path, capsys):
    """The file cannot keep a reason (comments are rewritten), so the reason goes in the PR."""
    _put(tmp_path, "src/new.py", cfs.LIMIT + 1)
    _put_baseline(tmp_path, {})
    cfs.main(["--root", str(tmp_path)])
    cfs.main(["--root", str(tmp_path), "--write-baseline"])
    out = capsys.readouterr().out
    assert "with a reason" not in out and "in the PR" in out
