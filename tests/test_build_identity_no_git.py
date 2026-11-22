# SPDX-License-Identifier: GPL-3.0-or-later
"""The release container has no git and builds from a `git archive` export (#1477 follow-up).

The caller (release/cut.sh on the host) knows the exact commit it exported and passes it in
NADARE_SOURCE_COMMIT. Without that, a git-less build is unverified, as before.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_identity", _REPO / "packaging/build_identity.py"
)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)

SHA = "0123456789abcdef0123456789abcdef01234567"


def _no_git(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(identity.subprocess, "check_output", missing)


def test_without_git_the_callers_commit_is_used(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    monkeypatch.setenv("NADARE_SOURCE_COMMIT", SHA)
    assert identity.source_commit(tmp_path) == SHA


def test_without_git_and_without_a_commit_it_stays_unknown(tmp_path, monkeypatch):
    _no_git(monkeypatch)
    monkeypatch.delenv("NADARE_SOURCE_COMMIT", raising=False)
    assert identity.source_commit(tmp_path) == "unknown"


@pytest.mark.parametrize("bad", ["", "abc", "Z" * 40, SHA[:-1], SHA + "0", f"{SHA}\n"])
def test_a_malformed_callers_commit_is_not_trusted(tmp_path, monkeypatch, bad):
    _no_git(monkeypatch)
    monkeypatch.setenv("NADARE_SOURCE_COMMIT", bad)
    assert identity.source_commit(tmp_path) == "unknown"


def test_in_a_git_checkout_the_callers_commit_must_match_head(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "-qm", "c"],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setenv("NADARE_SOURCE_COMMIT", head)
    assert identity.source_commit(tmp_path) == head
    monkeypatch.setenv("NADARE_SOURCE_COMMIT", SHA)
    assert identity.source_commit(tmp_path) == "unknown"
