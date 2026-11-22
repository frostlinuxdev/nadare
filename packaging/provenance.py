#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Write unsigned build provenance for a release asset (#1594).

The output is an in-toto v1 statement with the SLSA provenance v1 predicate:

    python packaging/provenance.py dist/Nadare-<version>-x86_64.AppImage \\
        --commit "$(git rev-parse HEAD)" --command "scripts/build_appimage.sh" \\
        --out dist/Nadare-<version>-x86_64.AppImage.intoto.json

It records the asset's SHA-256, the source commit, the builder name and the
command that produced the file. It is not signed: signing is the release
signing machine's job. It reads local files only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

BUILDER_ID = "nadare-release-scripts"
BUILD_TYPE = "https://nadare.app/build/release-scripts/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


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


def _commit(value: str) -> str:
    if not _COMMIT.fullmatch(value):
        raise ValueError(f"commit must be a 40-character git SHA: {value!r}")
    return value.lower()


def invocation_id(digest: str, commit: str, command: str) -> str:
    """A stable invocation id: the same asset built the same way gets the same id."""
    key = f"nadare-provenance:{digest}:{commit}:{command}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def build_statement(
    asset: Path,
    *,
    commit: str,
    command: str = "",
    timestamp: str | None = None,
) -> dict:
    """The in-toto statement for ``asset`` built from ``commit``."""
    commit = _commit(commit)
    if timestamp is None:
        timestamp = timestamp_from_env()
    digest = file_sha256(asset)
    metadata: dict = {"invocationId": invocation_id(digest, commit, command)}
    if timestamp:
        metadata["startedOn"] = timestamp
        metadata["finishedOn"] = timestamp
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": asset.name, "digest": {"sha256": digest}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {"command": command},
                "internalParameters": {},
                "resolvedDependencies": [{"digest": {"gitCommit": commit}}],
            },
            "runDetails": {
                "builder": {"id": BUILDER_ID},
                "metadata": metadata,
            },
        },
    }


def write_statement(
    asset: Path,
    out: Path,
    *,
    commit: str,
    command: str = "",
    timestamp: str | None = None,
) -> Path:
    if out.exists() and out.samefile(asset):
        raise ValueError("output must not overwrite the asset")
    statement = build_statement(
        asset, commit=commit, command=command, timestamp=timestamp
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(statement, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write unsigned in-toto/SLSA provenance for a built asset"
    )
    parser.add_argument("asset", type=Path, help="the built file to describe")
    parser.add_argument("--commit", required=True, help="the source git commit")
    parser.add_argument(
        "--command", default="", help="the build command line that produced the asset"
    )
    parser.add_argument("--out", required=True, type=Path, help="where to write the JSON")
    args = parser.parse_args(argv)
    try:
        write_statement(
            args.asset, args.out, commit=args.commit, command=args.command
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"provenance: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
