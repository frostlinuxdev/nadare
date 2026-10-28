# SPDX-License-Identifier: GPL-3.0-or-later
"""One dependency set: uv.lock is the truth; CI and requirements.txt follow it (#588)."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock() -> dict[str, str]:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_norm(pkg["name"]): pkg["version"] for pkg in data["package"]}


def _requirements() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)(\s*;.*)?", line)
        if match:
            pins[_norm(match.group(1))] = match.group(2)
    return pins


def _declared() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return [
        _norm(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
        for requirement in project["dependencies"]
    ]


def test_the_lock_records_the_current_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert _lock()[_norm(project["name"])] == project["version"]


def test_requirements_pin_every_declared_dependency_exactly():
    pinned = _requirements()
    missing = [name for name in _declared() if name not in pinned]
    assert missing == []


def test_requirements_agree_with_the_lock():
    lock = _lock()
    wrong = {
        name: (version, lock.get(name))
        for name, version in _requirements().items()
        if lock.get(name) != version
    }
    assert wrong == {}


def test_requirements_leave_nothing_floating():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    floating = [
        line
        for line in text.splitlines()
        if re.match(r"[A-Za-z]", line) and "==" not in line.split("#", 1)[0]
    ]
    assert floating == []


def test_the_build_script_tells_you_to_use_the_lock():
    text = (ROOT / "scripts/build_appimage.sh").read_text(encoding="utf-8")
    assert "uv sync --frozen --extra build" in text


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_uv_lock_check_passes():
    result = subprocess.run(
        ["uv", "lock", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_requirements_are_the_whole_runtime_set_of_the_lock():
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--no-annotate",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    exported = {
        _norm(match.group(1)): match.group(2)
        for line in result.stdout.splitlines()
        if (match := re.match(r"([A-Za-z0-9._-]+)==([^\s;]+)", line))
    }
    assert _requirements() == exported
