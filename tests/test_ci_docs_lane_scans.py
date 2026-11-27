# SPDX-License-Identifier: GPL-3.0-or-later
"""PR CI: a docs-only change runs only the tests that read the docs, and the public-text scans
(names, must-not-say content) run on every PR instead of only at release time (2026-11-27: two
comments naming a test machine stopped a release cut)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pick = _load("ci_pick_tests")
scan = _load("check_public_text")


# --- picking the tests ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["README.md", "docs/INSTALL.md", "docs/images/install/01-downloads.webp", "LICENSE"]
)
def test_docs_paths_count_as_docs(path):
    assert pick.is_doc(path)


@pytest.mark.parametrize(
    "path",
    ["src/nadare/app.py", "tests/test_x.py", "pyproject.toml", "uv.lock", "src/nadare/x.md"],
)
def test_code_and_packaged_files_do_not(path):
    assert not pick.is_doc(path)


TESTS = {
    "tests/test_public_docs.py": "text = (ROOT / 'docs/INSTALL.md').read_text()",
    "tests/test_licence.py": "assert (ROOT / 'LICENSE').is_file()",
    "tests/test_readme.py": "README.md",
    "tests/test_app.py": "import nadare.app",
}


def test_a_docs_only_change_runs_the_tests_that_read_docs():
    lane, targets = pick.pick(["docs/INSTALL.md", "README.md"], TESTS)
    assert lane == "docs"
    assert targets == ["tests/test_licence.py", "tests/test_public_docs.py", "tests/test_readme.py"]


def test_any_code_change_runs_everything():
    lane, targets = pick.pick(["docs/INSTALL.md", "src/nadare/app.py"], TESTS)
    assert (lane, targets) == ("full", ["tests"])


def test_no_changed_files_runs_everything():
    # An empty diff means the diff could not be read properly: never run less.
    assert pick.pick([], TESTS) == ("full", ["tests"])


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "b29")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_docs.py").write_text("open('docs/A.md')\n")
    (tmp_path / "tests/test_app.py").write_text("import app\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/b29", "HEAD")
    _git(tmp_path, "switch", "-q", "-c", "pr")
    return tmp_path


def test_main_reads_the_diff_against_the_base(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs/A.md").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")
    assert pick.main(["b29"], cwd=repo, fetch=False) == 0
    assert capsys.readouterr().out.splitlines() == ["lane=docs", "targets=tests/test_docs.py"]


def test_main_falls_back_to_everything_when_git_fails(tmp_path, capsys):
    # No repository at all: the safe answer is the full suite, never an error.
    assert pick.main(["b29"], cwd=tmp_path, fetch=False) == 0
    assert capsys.readouterr().out.splitlines() == ["lane=full", "targets=tests"]


def test_main_without_a_base_runs_everything(tmp_path, capsys):
    # Pushes and scheduled runs have no base branch.
    assert pick.main([""], cwd=_repo(tmp_path), fetch=False) == 0
    assert capsys.readouterr().out.splitlines() == ["lane=full", "targets=tests"]


# --- the public-text scan ------------------------------------------------------------------


def _scan_repo(tmp_path, files):
    _git(tmp_path, "init", "-q")
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(tmp_path, "add", "-A")
    return tmp_path


def test_scan_is_skipped_without_the_name_list(tmp_path, capsys):
    # The public (GitHub) CI has no list: it must not fail or pretend it scanned.
    repo = _scan_repo(tmp_path, {"a.py": "x = 1\n"})
    assert scan.main(cwd=repo, env={}) == 0
    assert "skipped" in capsys.readouterr().out


def test_scan_finds_a_private_name_in_tests_too(tmp_path, capsys):
    repo = _scan_repo(tmp_path, {"tests/test_a.py": "# ran on Boxname-7 today\n", "a.py": "x\n"})
    env = {"PRIVATE_NAMES": "# machines\nboxname-7  # the test desktop\n\n"}
    assert scan.main(cwd=repo, env=env) == 1
    out = capsys.readouterr().out
    assert "NAME-FAIL tests/test_a.py:1" in out
    # The log names the place, not the private word.
    assert "boxname" not in out.lower()


def test_scan_finds_must_not_say_content_outside_tests(tmp_path, capsys):
    words = "we " + "decompi" + "led it"
    repo = _scan_repo(tmp_path, {"docs/A.md": words + "\n", "tests/test_a.py": words + "\n"})
    assert scan.main(cwd=repo, env={"PRIVATE_NAMES": "nothing-here"}) == 1
    out = capsys.readouterr().out
    assert "CONTENT-FAIL docs/A.md:1" in out
    assert "tests/test_a.py" not in out


def test_public_only_paths_are_left_out(tmp_path, capsys):
    words = "reverse " + "engineering notes"
    repo = _scan_repo(tmp_path, {"research/a.md": words + "\n", "scripts/x/README.md": "boxname\n"})
    env = {"PRIVATE_NAMES": "boxname", "PUBLIC_ONLY": "research/\nscripts/x/  # internal\n"}
    assert scan.main(cwd=repo, env=env) == 0
    assert "PUBLIC-TEXT: clean" in capsys.readouterr().out


def test_this_tree_is_clean_without_names():
    # The content half needs no secret: the tree as it stands passes it (public-only paths aside).
    # The probe name is built here: written out, git grep would find it in this file.
    env = {
        "PRIVATE_NAMES": "zz-no-such" + "-name-zz",
        "PUBLIC_ONLY": "scripts/nikke_launcherless/\nresearch/",
    }
    assert scan.main(cwd=ROOT, env=env) == 0


# --- the workflow --------------------------------------------------------------------------


def _jobs(rel):
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_lint_runs_the_public_text_scan_with_the_secrets(rel):
    step = next(
        s for s in _jobs(rel)["lint"]["steps"] if "check_public_text.py" in s.get("run", "")
    )
    assert step["env"]["PRIVATE_NAMES"] == "${{ secrets.PRIVATE_NAMES }}"
    assert step["env"]["PUBLIC_ONLY"] == "${{ secrets.PUBLIC_ONLY }}"
    assert not step.get("continue-on-error")


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_test_job_runs_what_was_picked(rel):
    steps = _jobs(rel)["test"]["steps"]
    pick_step = next(s for s in steps if "ci_pick_tests.py" in s.get("run", ""))
    assert pick_step["id"] == "pick"
    assert "github.base_ref" in pick_step["run"]
    test = next(s for s in steps if s["name"] == "Test")
    assert "steps.pick.outputs.targets" in test["run"]
    lifecycle = next(s for s in steps if s["name"].startswith("Lifecycle lane"))
    assert lifecycle["if"] == "steps.pick.outputs.lane != 'docs'"
