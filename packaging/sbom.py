#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Write a CycloneDX 1.6 SBOM for a built release asset (#1594).

The build already knows what goes into an asset: the hashed Python requirements,
the native libraries bundled next to them, and Nadare itself. This script turns
that knowledge into one deterministic JSON file:

    python packaging/sbom.py dist/Nadare-<version>-x86_64.AppImage \\
        --out dist/Nadare-<version>-x86_64.AppImage.cdx.json

It reads local files only; it never resolves or downloads a dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTERNAL = Path("AppDir/usr/bin/_internal")
_BUILDER_NAME = "nadare-release-scripts"
_HASH = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Requirement:
    name: str
    version: str
    hashes: tuple[str, ...] = ()
    marker: str | None = None


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def timestamp_from_env(env=None) -> str | None:
    """SOURCE_DATE_EPOCH as an RFC 3339 UTC timestamp, or None when unset."""
    value = (env if env is not None else os.environ).get("SOURCE_DATE_EPOCH")
    if not value:
        return None
    try:
        seconds = int(value)
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"SOURCE_DATE_EPOCH is not a supported Unix timestamp: {value!r}") from exc


def _logical_lines(text: str):
    """Join pip's backslash continuations and drop comments and blank lines."""
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        buffer += line
        yield buffer.strip()
        buffer = ""
    if buffer.strip():
        yield buffer.strip()


def parse_requirements(path: Path) -> list[Requirement]:
    """Read a hashed ``pip`` requirements file into sorted components."""
    requirements: list[Requirement] = []
    for line in _logical_lines(path.read_text(encoding="utf-8")):
        hashes = tuple(sorted({match.lower() for match in _HASH.findall(line)}))
        body = _HASH.sub("", line).strip()
        requirement, _, marker = body.partition(";")
        requirement = requirement.strip()
        name, separator, version = requirement.partition("==")
        if not separator or not name.strip() or not version.strip():
            raise ValueError(f"unsupported requirements line: {line!r}")
        name = name.strip().split("[", 1)[0]
        requirements.append(
            Requirement(
                name=name,
                version=version.strip(),
                hashes=hashes,
                marker=marker.strip() or None,
            )
        )
    return sorted(
        set(requirements),
        key=lambda entry: (
            _normalise(entry.name), entry.version, entry.marker or "", entry.hashes, entry.name
        ),
    )


def _collect_licences():
    """Load the licence collector so both scripts see the same bundled libraries."""
    path = DEFAULT_ROOT / "scripts" / "collect_licences.py"
    spec = importlib.util.spec_from_file_location("nadare_collect_licences", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundled_libraries(internal: Path) -> list[str]:
    """The native libraries the AppImage bundles, as ``collect_licences.py`` finds them."""
    return _collect_licences()._bundled_libraries(internal)


def _library_path(internal: Path, name: str) -> Path | None:
    for candidate in (internal / name, internal / "fallback-libs" / name):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_component(requirement: Requirement) -> dict:
    purl = f"pkg:pypi/{_normalise(requirement.name)}@{quote(requirement.version, safe='')}"
    identity = json.dumps(
        [requirement.name, requirement.version, requirement.marker, requirement.hashes]
    )
    component = {
        "type": "library",
        "bom-ref": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "name": requirement.name,
        "version": requirement.version,
        "purl": purl,
    }
    if requirement.hashes:
        component["hashes"] = [
            {"alg": "SHA-256", "content": digest} for digest in requirement.hashes
        ]
    if requirement.marker:
        component["properties"] = [
            {"name": "nadare:requirements-marker", "value": requirement.marker}
        ]
    return component


def _library_component(internal: Path, name: str) -> dict:
    component = {
        "type": "library",
        "bom-ref": f"nadare:native:{name}",
        "name": name,
    }
    path = _library_path(internal, name)
    if path is not None:
        component["hashes"] = [{"alg": "SHA-256", "content": file_sha256(path)}]
        component["properties"] = [
            {
                "name": "nadare:bundled-path",
                "value": path.relative_to(internal).as_posix(),
            }
        ]
    return component


def serial_number(digest: str) -> str:
    """A stable CycloneDX serial number derived from the asset digest."""
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"not a SHA-256 digest: {digest!r}")
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'nadare-sbom:{digest}')}"


def _project(root: Path) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _nadare_component(project: dict) -> dict:
    licence = project.get("license")
    if isinstance(licence, dict):
        licence = licence.get("text")
    name = project["name"]
    version = project["version"]
    purl = f"pkg:pypi/{_normalise(name)}@{quote(version, safe='')}"
    component = {
        "type": "application",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
    }
    if licence:
        component["licenses"] = [{"expression": licence}]
    return component


def build_sbom(
    asset: Path,
    *,
    requirements: Path,
    internal: Path,
    root: Path = DEFAULT_ROOT,
    timestamp: str | None = None,
) -> dict:
    """The CycloneDX 1.6 document for ``asset`` and the components the build knows."""
    if timestamp is None:
        timestamp = timestamp_from_env()
    digest = file_sha256(asset)
    project = _project(root)
    python = parse_requirements(requirements)
    native = sorted(bundled_libraries(internal))

    metadata: dict = {
        "tools": {
            "components": [
                {"type": "application", "name": _BUILDER_NAME, "version": "1"}
            ]
        },
        "component": {
            "type": "application",
            "bom-ref": f"nadare:asset:{digest}",
            "name": asset.name,
            "hashes": [{"alg": "SHA-256", "content": digest}],
        },
    }
    if timestamp:
        metadata["timestamp"] = timestamp

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial_number(digest),
        "version": 1,
        "metadata": metadata,
        "components": [
            _nadare_component(project),
            *[_requirement_component(entry) for entry in python],
            *[_library_component(internal, name) for name in native],
        ],
    }


def write_sbom(
    asset: Path,
    out: Path,
    *,
    requirements: Path,
    internal: Path,
    root: Path = DEFAULT_ROOT,
    timestamp: str | None = None,
) -> Path:
    if out.exists() and out.samefile(asset):
        raise ValueError("output must not overwrite the asset")
    document = build_sbom(
        asset,
        requirements=requirements,
        internal=internal,
        root=root,
        timestamp=timestamp,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a CycloneDX SBOM for a built asset")
    parser.add_argument("asset", type=Path, help="the built file to describe")
    parser.add_argument("--out", required=True, type=Path, help="where to write the JSON")
    parser.add_argument(
        "--requirements",
        type=Path,
        help="hashed requirements.txt (default: requirements.txt in the repository)",
    )
    parser.add_argument(
        "--internal",
        type=Path,
        help="the bundle's _internal folder (default: AppDir/usr/bin/_internal)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="source checkout to read pyproject.toml from",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    requirements = args.requirements or root / "requirements.txt"
    internal = args.internal or DEFAULT_INTERNAL
    try:
        write_sbom(
            args.asset,
            args.out,
            requirements=requirements,
            internal=internal,
            root=root,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"sbom: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
