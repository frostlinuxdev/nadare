# SPDX-License-Identifier: GPL-3.0-or-later
"""Build identity checks use synthetic locks and installed metadata only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def identity():
    spec = importlib.util.spec_from_file_location(
        "build_identity_test", ROOT / "packaging/build_identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / "uv.lock"
    path.write_text("""version = 1
[[package]]
name = "nadare-launcher"
version = "1.2.3"
source = { editable = "." }
dependencies = [{name = "runtime"}]
[package.optional-dependencies]
build = [{name = "builder"}]
[[package]]
name = "runtime"
version = "2.0"
dependencies = [{name = "linux-lib", marker = "sys_platform == 'linux'"},
                {name = "windows-lib", marker = "sys_platform == 'win32'"}]
[[package]]
name = "builder"
version = "3.0"
[[package]]
name = "linux-lib"
version = "4.0"
[[package]]
name = "windows-lib"
version = "5.0"
""")
    return path


def test_environment_checks_transitive_runtime_and_build_versions(identity, lock):
    installed = {"nadare-launcher": "1.2.3", "runtime": "2.0", "builder": "3.0", "linux-lib": "4.0"}
    assert identity.validate_packages(lock, installed.__getitem__) == installed
    for name in installed:
        bad = {**installed, name: "stale"}
        with pytest.raises(ValueError, match=name):
            identity.validate_packages(lock, bad.__getitem__)


def test_environment_rejects_missing_dependency(identity, lock):
    with pytest.raises(ValueError, match="runtime"):
        identity.validate_packages(lock, {"nadare-launcher": "1.2.3", "builder": "3.0"}.__getitem__)


def test_digest_rejects_external_symlinks(identity, tmp_path):
    (tmp_path / "escape").symlink_to("../outside")
    with pytest.raises(ValueError, match="symlink"):
        identity.payload_digest(tmp_path)


def test_appimage_checks_environment_before_build_and_emits_after_transformations():
    script = (ROOT / "scripts/build_appimage.sh").read_text()
    assert script.index("build_identity.py prepare") < script.index('"$PY" -m PyInstaller')
    assert script.index("build_identity.py emit") > script.index("strip --strip-all")
    assert script.index("build_identity.py emit") < script.index('"${update_args[@]}" AppDir')


def test_source_commit_states_are_explicit(identity, tmp_path, monkeypatch):
    answers = iter([str(tmp_path), "a" * 40 + "\n", "",
                    str(tmp_path), "a" * 40 + "\n", " M tracked.py\n"])
    monkeypatch.setattr(identity.subprocess, "check_output", lambda *a, **k: next(answers))
    assert identity.source_commit(tmp_path) == "a" * 40
    assert identity.source_commit(tmp_path) == "dirty"

    def missing(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(identity.subprocess, "check_output", missing)
    assert identity.source_commit(tmp_path) == "unknown"


def test_environment_rejects_stale_source_location(identity, tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        identity.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin="/other/nadare.py"),
    )
    with pytest.raises(ValueError, match="source location"):
        identity.checked_environment(tmp_path)


def test_emitter_roundtrip_and_changed_environment(identity, tmp_path, monkeypatch):
    import json
    import sys

    state = {
        "schema": 1,
        "verified": True,
        "version": "1.2.3",
        "source_commit": "a" * 40,
        "uv_lock_sha256": "b" * 64,
        "environment": {"nadare-launcher": "1.2.3"},
    }
    monkeypatch.setattr(identity, "checked_environment", lambda *a, **k: dict(state))
    environment = tmp_path / "environment.json"
    argv = ["identity", "prepare", "--environment", str(environment)]
    monkeypatch.setattr(sys, "argv", argv)
    identity.main()
    assert json.loads(environment.read_text()) == state
    appdir = tmp_path / "AppDir"
    (appdir / "usr/bin").mkdir(parents=True)
    (appdir / "usr/bin/nadare").write_bytes(b"fixture")
    argv[1] = "emit"
    argv.extend(["--appdir", str(appdir)])
    identity.main()
    record = json.loads((appdir / identity.MARKER).read_text())
    assert record["payload_sha256"] == identity.payload_digest(appdir / "usr/bin")
    assert record["transformations"] == identity.APPIMAGE_TRANSFORMATIONS
    state["uv_lock_sha256"] = "c" * 64
    with pytest.raises(SystemExit):
        identity.main()
    assert json.loads((appdir / identity.MARKER).read_text()) == record


def test_source_archive_does_not_inherit_a_parent_repository_commit(
    identity, tmp_path, monkeypatch,
):
    def git_output(argv, **kwargs):
        if argv[-1] == "--show-toplevel":
            return str(tmp_path.parent)
        if argv[-1] == "HEAD":
            return "a" * 40
        return ""

    monkeypatch.setattr(identity.subprocess, "check_output", git_output)
    assert identity.source_commit(tmp_path) == "unknown"
