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

#: The CI-file checks hold for the internal tree only. The public tree keeps its own hand-written
#: CI (it installs requirements.txt, whose hashes the tests below still check), so a release cut
#: carries this file unchanged and these checks skip there. TESTING.md never goes public.
internal_ci = pytest.mark.skipif(
    not (ROOT / "TESTING.md").is_file(),
    reason="public tree: its CI is kept by hand and does not install through uv",
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock() -> dict[str, str]:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_norm(pkg["name"]): pkg["version"] for pkg in data["package"]}


_PIN = re.compile(r"([A-Za-z0-9._-]+)==([^\s;\\]+)(\s*;[^\\]*)?\s*\\?")
_HASH = re.compile(r"--hash=(sha256:[0-9a-f]{64})\s*\\?")


def _hashed(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    """``{name: (version, hashes)}`` of a pip requirements file with ``--hash`` lines (#1502)."""
    found: dict[str, tuple[str, set[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("--hash") else raw.strip()
        if not line:
            continue
        if match := _PIN.fullmatch(line):
            current = _norm(match.group(1))
            found[current] = (match.group(2), set())
        elif (match := _HASH.fullmatch(line)) and current is not None:
            found[current][1].add(match.group(1))
    return {name: (version, frozenset(hashes)) for name, (version, hashes) in found.items()}


def _requirements() -> dict[str, str]:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    return {name: version for name, (version, _hashes) in _hashed(text).items()}


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


@internal_ci
@pytest.mark.parametrize("workflow", [".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"])
def test_ci_installs_from_the_lock(workflow):
    text = (ROOT / workflow).read_text(encoding="utf-8")
    assert "uv sync --frozen" in text
    assert "pip install -r requirements.txt" not in text


@internal_ci
@pytest.mark.parametrize("workflow", [".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"])
def test_ci_runs_the_tests_through_uv_not_through_a_path_hack(workflow):
    text = (ROOT / workflow).read_text(encoding="utf-8")
    assert "uv run --frozen" in text
    assert "GITHUB_PATH" not in text


@internal_ci
def test_the_two_ci_files_stay_identical():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    forgejo = (ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    assert github == forgejo


@internal_ci
def test_ci_checks_that_the_lock_is_current():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "uv lock --check" in text


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
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-annotate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    exported = _hashed(result.stdout)
    assert exported, "the export parsed to nothing"
    # Versions and every hash, so a regenerated lock cannot leave stale hashes behind.
    assert _hashed((ROOT / "requirements.txt").read_text(encoding="utf-8")) == exported


def test_every_pinned_requirement_carries_its_hashes():
    """pip enters hash-checking mode with these: a swapped file on PyPI is refused (#1502)."""
    pins = _hashed((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    assert pins
    assert [name for name, (_version, hashes) in pins.items() if not hashes] == []


def test_the_requirements_hashes_are_the_locks():
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, set[str]] = {}
    for pkg in data["package"]:
        files = [*pkg.get("wheels", []), *([pkg["sdist"]] if "sdist" in pkg else [])]
        locked[_norm(pkg["name"])] = {f["hash"] for f in files if "hash" in f}
    pins = _hashed((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    stray = {name: sorted(hashes - locked.get(name, set())) for name, (_v, hashes) in pins.items()}
    assert {name: extra for name, extra in stray.items() if extra} == {}


def test_the_header_names_the_hashed_export_command():
    head = (ROOT / "requirements.txt").read_text(encoding="utf-8").split("\n", 6)
    command = next(line for line in head if "uv export" in line)
    assert "--no-hashes" not in command


# ---------------------------------------------------------------------------
# #1553: the standalone AppImage build and the dev.sh pip fallback use the lock
# ---------------------------------------------------------------------------
def _run_build_script(tmp_path: Path, extra_env: dict[str, str]) -> tuple[int, str, str]:
    """Run build_appimage.sh with a fake ``uv`` that records its call and stops the script.

    An invalid NADARE_UPDATE_INFO makes the script stop right after it picks the interpreter,
    before anything slow, when ``uv`` was not called.
    """
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "uv.log"
    (fake / "uv").write_text(
        '#!/bin/sh\nprintf "%s\\n" "UV_PROJECT_ENVIRONMENT=$UV_PROJECT_ENVIRONMENT" "$@" '
        '> "$FAKE_UV_LOG"\nexit 3\n'
    )
    (fake / "uv").chmod(0o755)
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_UV_LOG": str(log),
        "NADARE_UPDATE_INFO": "not-valid",
        **extra_env,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/build_appimage.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    called = log.read_text() if log.exists() else ""
    return result.returncode, called, result.stderr


def test_a_standalone_build_syncs_the_locked_build_env_first(tmp_path):
    code, called, _ = _run_build_script(tmp_path, {})
    assert code == 3, "the script must stop when the locked sync fails"
    args = called.split("\n")
    assert args[1:5] == ["sync", "--frozen", "--extra", "build"]
    # Its own environment: syncing the build set must not strip the dev tools from .venv.
    assert args[0] == "UV_PROJECT_ENVIRONMENT=.venv-build"


def test_an_explicit_python_skips_the_sync(tmp_path):
    """The release container syncs with --locked itself and sets PYTHON."""
    code, called, _ = _run_build_script(tmp_path, {"PYTHON": "/bin/false"})
    assert called == ""
    assert code == 1


def test_the_dev_requirements_are_the_locks_dev_set():
    pins = _hashed((ROOT / "requirements-dev.txt").read_text(encoding="utf-8"))
    assert {"pytest", "ruff", "pyqt6"} <= set(pins)
    assert [name for name, (_v, hashes) in pins.items() if not hashes] == []


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_the_dev_requirements_match_a_fresh_export():
    result = subprocess.run(
        ["uv", "export", "--frozen", "--extra", "dev", "--no-emit-project", "--no-annotate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    exported = _hashed(result.stdout)
    assert exported
    assert _hashed((ROOT / "requirements-dev.txt").read_text(encoding="utf-8")) == exported


def test_the_dev_sh_pip_fallback_installs_hashed_pins_only():
    text = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    fallback = text[text.index("falling back to pip") :]
    fallback = fallback[: fallback.index("fi\n")]
    assert "-r requirements-dev.txt" in fallback
    assert '".[dev]"' not in fallback
    assert "--no-deps -e ." in fallback
