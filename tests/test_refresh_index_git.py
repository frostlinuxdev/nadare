# SPDX-License-Identifier: GPL-3.0-or-later
"""The index refresher must recover from a generated commit whose push failed."""

from __future__ import annotations

import subprocess
from pathlib import Path

HELPER = Path(__file__).parents[1] / "scripts" / "index_git.sh"
ENV_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *ENV_ID, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _sh(func: str, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{HELPER}"; {func} "{repo}"'],
        check=False,
        capture_output=True,
        text=True,
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    a, b = tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(origin), str(a)], check=True)
    (a / "one.txt").write_text("1\n")
    _git(a, "add", ".")
    _git(a, "commit", "-qm", "init")
    _git(a, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True)
    for repo in (a, b):
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.invalid")
    return origin, a, b


def test_pull_rebases_a_local_generated_commit_onto_newer_origin(tmp_path):
    origin, a, b = _setup(tmp_path)
    (b / "gen.json").write_text("{}\n")
    _git(b, "add", ".")
    _git(b, "commit", "-qm", "generated")
    (a / "other.txt").write_text("x\n")
    _git(a, "add", ".")
    _git(a, "commit", "-qm", "ci")
    _git(a, "push", "-q", "origin", "HEAD:main")

    result = _sh("index_pull", b)

    assert result.returncode == 0, result.stderr
    assert (b / "other.txt").exists() and (b / "gen.json").exists()
    assert _git(b, "status", "--porcelain") == ""


def test_pull_that_cannot_rebase_aborts_and_fails(tmp_path):
    origin, a, b = _setup(tmp_path)
    (b / "one.txt").write_text("mine\n")
    _git(b, "commit", "-qam", "generated")
    (a / "one.txt").write_text("theirs\n")
    _git(a, "commit", "-qam", "ci")
    _git(a, "push", "-q", "origin", "HEAD:main")

    result = _sh("index_pull", b)

    assert result.returncode != 0
    assert not (b / ".git" / "rebase-merge").exists()
    assert not (b / ".git" / "rebase-apply").exists()


def test_push_retries_once_after_rebasing_when_origin_moved(tmp_path):
    origin, a, b = _setup(tmp_path)
    (b / "gen.json").write_text("{}\n")
    _git(b, "add", ".")
    _git(b, "commit", "-qm", "generated")
    (a / "other.txt").write_text("x\n")
    _git(a, "add", ".")
    _git(a, "commit", "-qm", "ci")
    _git(a, "push", "-q", "origin", "HEAD:main")

    result = _sh("index_push", b)

    assert result.returncode == 0, result.stderr
    assert _git(origin, "ls-tree", "--name-only", "main").split() == [
        "gen.json",
        "one.txt",
        "other.txt",
    ]
