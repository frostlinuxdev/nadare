#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Checked AppImage build identity; packaging only, never imported by the app."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path

from packaging.markers import Marker

MARKER = "build-identity.json"
APPIMAGE_TRANSFORMATIONS = [
    "PyInstaller onedir bundle",
    "add host GLVND and XCB libraries",
    "move fontconfig and freetype to fallback-libs",
    "remove Qt PDF library and image plugin",
    "remove zstandard CFFI backend",
    "strip --strip-debug shared libraries (best effort)",
    "strip --strip-all libpython",
    "remove GTK3 platform theme plugin",
]


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def payload_digest(root: Path, *, exclude: set[str] | None = None) -> str:
    """Hash sorted JSON records of paths, modes, types and bytes/link targets.

    Never follow symlinks; reject links escaping the payload, and special files.
    Directory modes are excluded because staging creates new metadata directories.
    """
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in (exclude or set()):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            try:
                path.resolve().relative_to(root)
            except (ValueError, RuntimeError) as exc:
                raise ValueError(f"unsafe payload symlink: {relative}") from exc
            record = [relative, "symlink", os.readlink(path)]
        elif stat.S_ISREG(mode):
            record = [relative, "file", stat.S_IMODE(mode), file_sha256(path)]
        elif stat.S_ISDIR(mode):
            record = [relative, "directory"]
        else:
            raise ValueError(f"unsupported payload entry: {relative}")
        encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        digest.update((encoded + "\n").encode())
    return digest.hexdigest()


def validate_packages(lock: Path, installed=importlib.metadata.version) -> dict[str, str]:
    """Check the active runtime + build dependency closure, including lock markers."""
    data = tomllib.loads(lock.read_text())
    packages = {}
    for package in data["package"]:
        packages.setdefault(package["name"], []).append(package)
    pending = [{"name": "nadare-launcher"}]
    checked = {}
    while pending:
        dependency = pending.pop()
        if dependency.get("marker") and not Marker(dependency["marker"]).evaluate(
            {"extra": "build"}
        ):
            continue
        name = dependency["name"]
        if name in checked:
            continue
        candidates = [
            p
            for p in packages.get(name, [])
            if "version" not in dependency or p["version"] == dependency["version"]
        ]
        if len(candidates) != 1:
            raise ValueError(f"{name}: ambiguous or missing lock entry")
        package = candidates[0]
        try:
            actual = installed(name)
        except (importlib.metadata.PackageNotFoundError, KeyError) as exc:
            raise ValueError(f"{name}: not installed") from exc
        if actual != package["version"]:
            raise ValueError(f"{name}: installed {actual}, locked {package['version']}")
        checked[name] = actual
        pending.extend(package.get("dependencies", []))
        if name == "nadare-launcher":
            pending.extend(package.get("optional-dependencies", {}).get("build", []))
    return dict(sorted(checked.items()))


def _callers_commit() -> str | None:
    """NADARE_SOURCE_COMMIT: the exact commit the caller exported (a 40-hex sha), if valid."""
    value = os.environ.get("NADARE_SOURCE_COMMIT", "")
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def source_commit(root: Path) -> str:
    """The commit this tree was built from, ``dirty`` or ``unknown``.

    In a git checkout, HEAD (and a clean tree) decides; a NADARE_SOURCE_COMMIT given there must
    match HEAD. The release container has no git and builds a ``git archive`` export of one
    commit, so without a repository the caller's NADARE_SOURCE_COMMIT is used (#1477 follow-up).
    """
    callers = _callers_commit()
    try:
        top = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        # No git, or not a repository: only the caller can say which commit this is.
        return callers or "unknown"
    try:
        if Path(top).resolve() != root.resolve():
            return callers or "unknown"
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    if dirty:
        return "dirty"
    if callers is not None and callers != head:
        return "unknown"
    return head


def checked_environment(root: Path, *, allow_unverified: bool = False) -> dict:
    """Reject stale source installs and dependencies even with an explicit PYTHON."""
    spec = importlib.util.find_spec("nadare")
    expected = root / "src/nadare/__init__.py"
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected.resolve():
        raise ValueError("nadare source location does not match the build tree")
    import nadare

    version = nadare.__version__
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if version != project["version"]:
        raise ValueError("nadare source version does not match pyproject.toml")
    environment = validate_packages(root / "uv.lock")
    if environment["nadare-launcher"] != version:
        raise ValueError("installed nadare version does not match the build tree")
    commit = source_commit(root)
    verified = bool(re.fullmatch(r"[0-9a-f]{40}", commit))
    if not verified and not allow_unverified:
        raise ValueError(
            "source identity is dirty/unknown; use --allow-unverified for development only"
        )
    return {
        "schema": 1,
        "verified": verified,
        "version": version,
        "source_commit": commit,
        "uv_lock_sha256": file_sha256(root / "uv.lock"),
        "environment": environment,
    }


def check_identity(
    appdir: Path,
    *,
    expected_version: str | None,
    expected_commit: str | None,
    expected_lock_sha256: str | None,
    allow_unverified: bool = False,
) -> tuple[dict | None, list[str], str]:
    """Check explicit consumer expectations before creating any output."""
    errors = []
    record = None
    try:
        record = json.loads((appdir / MARKER).read_text())
        if not isinstance(record, dict):
            raise ValueError("marker must be an object")
    except (OSError, ValueError) as exc:
        record = None
        errors.append(f"missing or malformed identity: {exc}")
    actual = payload_digest(appdir / "usr/bin")
    if record is not None:
        if record.get("schema") != 1 or record.get("verified") is not True:
            errors.append("identity schema/verified marker is not verified")
        if not isinstance(record.get("environment"), dict) or not record["environment"]:
            errors.append("identity lacks checked environment")
        if not isinstance(record.get("transformations"), list):
            errors.append("identity lacks transformations")
        for key, expected in (
            ("version", expected_version),
            ("source_commit", expected_commit),
            ("uv_lock_sha256", expected_lock_sha256),
        ):
            if not expected or record.get(key) != expected:
                errors.append(f"identity {key} does not match explicit expected input")
        if not re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_commit", ""))):
            errors.append("identity source commit is dirty/unknown or invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("uv_lock_sha256", ""))):
            errors.append("identity lock SHA-256 is invalid")
        if record.get("payload_sha256") != actual:
            errors.append("identity payload digest mismatch")
    if not all((expected_version, expected_commit, expected_lock_sha256)):
        errors.append("identity requires expected version, commit and lock SHA-256")
    if allow_unverified:
        errors.append("explicit unverified development override")
    if errors and not allow_unverified:
        raise ValueError("; ".join(errors))
    return record, errors, actual


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "emit"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--appdir", type=Path)
    parser.add_argument("--allow-unverified", action="store_true")
    args = parser.parse_args()
    try:
        record = checked_environment(args.root.resolve(), allow_unverified=args.allow_unverified)
        if args.action == "prepare":
            args.environment.parent.mkdir(parents=True, exist_ok=True)
            args.environment.write_text(json.dumps(record, indent=2) + "\n")
        else:
            if record != json.loads(args.environment.read_text()):
                raise ValueError("build source, lock or environment changed during bundling")
            if args.appdir is None:
                raise ValueError("emit requires --appdir")
            record["payload_sha256"] = payload_digest(args.appdir / "usr/bin")
            record["transformations"] = APPIMAGE_TRANSFORMATIONS
            (args.appdir / MARKER).write_text(json.dumps(record, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"build identity: {exc}\n")


if __name__ == "__main__":
    main()
