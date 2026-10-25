# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the index refresher's opt-in catalog staging boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts" / "stage_index_changes.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "index"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for path, text in (
        ("webview2/installer.json", "old webview2\n"),
        ("catalog/index.json", "old catalog\n"),
        ("unrelated.txt", "old unrelated\n"),
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _run_helper(repo: Path, catalog: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid")
    env.update(GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    return subprocess.run(
        ["bash", str(HELPER), str(repo), catalog],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_refresh_index_uses_dedicated_staging_guard():
    assert HELPER.is_file(), "refresh_index has no bounded staging helper"
    script = (ROOT / "scripts" / "refresh_index.sh").read_text()
    assert "stage_index_changes.sh" in script


def test_catalog_off_commits_only_generated_allowlist(tmp_path):
    repo = _repo(tmp_path)
    (repo / "webview2/installer.json").write_text("new webview2\n")
    (repo / "catalog/index.json").write_text("manual catalog edit\n")
    (repo / "unrelated.txt").write_text("manual staged edit\n")
    _git(repo, "add", "unrelated.txt")

    result = _run_helper(repo, "0")

    assert result.returncode == 0, result.stderr
    committed = set(_git(repo, "show", "--pretty=format:", "--name-only", "HEAD").splitlines())
    assert committed == {"webview2/installer.json"}
    staged = set(_git(repo, "diff", "--cached", "--name-only").splitlines())
    assert staged == {"unrelated.txt"}
    assert _git(repo, "diff", "--name-only") == "catalog/index.json"


def test_catalog_only_pending_change_does_not_make_opt_out_publish(tmp_path):
    repo = _repo(tmp_path)
    (repo / "catalog/index.json").write_text("manual catalog edit\n")
    _git(repo, "add", "catalog/index.json")
    before = _git(repo, "rev-parse", "HEAD")

    result = _run_helper(repo, "0")

    assert result.returncode == 3
    assert _git(repo, "rev-parse", "HEAD") == before


def test_catalog_on_can_commit_catalog_with_generated_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "webview2/installer.json").write_text("new webview2\n")
    (repo / "catalog/index.json").write_text("new catalog\n")

    result = _run_helper(repo, "1")

    assert result.returncode == 0, result.stderr
    committed = set(_git(repo, "show", "--pretty=format:", "--name-only", "HEAD").splitlines())
    assert committed == {"webview2/installer.json", "catalog/index.json"}
