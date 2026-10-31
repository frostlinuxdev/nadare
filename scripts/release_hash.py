#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Publish a release's AppImage hash in the Nadare index (#771).

Nadare's in-place update checks the new AppImage against the hash listed in
``app/releases.json`` of the index (and against the release's SHA256SUMS; the two must
agree). Run this once per release, then commit and push the index checkout::

    python scripts/release_hash.py ~/nadare-index 0.1.0b20 dist/Nadare-0.1.0b20-x86_64.AppImage
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

#: Newest releases kept in the document (older ones are never offered an in-place update).
KEEP = 30
DOC = Path("app") / "releases.json"


class ReleaseHashError(Exception):
    """The hash could not be published; the message says why."""


def _version_key(version: str) -> tuple[int, ...]:
    from nadare.core.updatecheck import version_key

    key = version_key(version)
    if key is None or version != version.strip() or version[:1] in ("v", "V") or "/" in version:
        raise ReleaseHashError(f"not a release version: {version!r} (use e.g. 0.1.0b20)")
    if version.count(".") < 2:
        raise ReleaseHashError(f"not a release version: {version!r} (use e.g. 0.1.0b20)")
    return key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(doc: Path) -> dict:
    if not doc.exists():
        return {"schema": 1, "releases": {}}
    try:
        data = json.loads(doc.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseHashError(f"{doc} is not valid JSON; fix or remove it first: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
        raise ReleaseHashError(f"{doc} is not a releases document; fix or remove it first")
    return data


def add_release(
    index_dir: str | os.PathLike[str], version: str, appimage: str | os.PathLike[str]
) -> Path:
    """Record ``appimage``'s SHA-256 for ``version``; returns the document written."""
    _version_key(version)
    image = Path(appimage)
    if not image.is_file():
        raise ReleaseHashError(f"{image} is not a file")
    digest = _sha256(image)
    doc = Path(index_dir) / DOC
    data = _load(doc)
    releases: dict = data["releases"]
    entry = releases.get(version)
    current = entry.get("x86_64", {}).get("sha256") if isinstance(entry, dict) else None
    if current and current != digest:
        raise ReleaseHashError(
            f"{version} is already published with a different hash; refusing to change it"
        )
    releases[version] = {"x86_64": {"sha256": digest}}
    newest = sorted(releases, key=_version_key)[-KEEP:]
    data["releases"] = {name: releases[name] for name in newest}
    data["schema"] = 1
    doc.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=doc.parent, prefix=".releases-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(tmp_name, doc)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return doc


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(__doc__)
        return 2
    try:
        written = add_release(args[0], args[1], args[2])
    except ReleaseHashError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"[+] wrote {written}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    raise SystemExit(main())
