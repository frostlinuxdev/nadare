#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Generate or update an index patch pin JSON file from a local patch zip.

No network access is performed; the downloaded archive is given via ``--zip``::

    python3 scripts/pin_patch.py --id kibotl-en --zip en.zip --index ../nadare-index
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nadare.core import atomicfile, patchspecs  # noqa: E402

_CHUNK = 1 << 20


class PinPatchError(RuntimeError):
    """Raised when a patch pin document cannot be built or validated."""


def _hash_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _hash_file(path: Path) -> tuple[str, int]:
    with open(path, "rb") as handle:
        return _hash_stream(handle)


def _before_pins_from_install(before_root: Path, dest: str) -> tuple[str, int]:
    root = before_root.expanduser().resolve()
    if not root.is_dir():
        raise PinPatchError(f"--before-from folder does not exist: {before_root}")
    target = (root / dest).resolve()
    if not target.is_relative_to(root):
        raise PinPatchError(f"destination escapes --before-from folder: {dest}")
    if not target.is_file():
        raise PinPatchError(f"pristine file missing in --before-from: {dest}")
    return _hash_file(target)


def build_pin_document(
    patch_id: str,
    zip_path: Path,
    index_dir: Path,
    *,
    version: str | None = None,
    url: str | None = None,
    before_from: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    found = patchspecs.builtin_by_id(patch_id)
    if found is None:
        raise PinPatchError(f"unknown patch id: {patch_id!r}")
    builtin, rule = found

    if not zip_path.is_file():
        raise PinPatchError(f"zip file not found: {zip_path}")

    target_path = index_dir / rule.index_file
    base_spec = builtin
    if target_path.is_file():
        try:
            existing_raw = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PinPatchError(f"cannot read existing index doc {target_path}: {exc}") from exc
        existing_spec = patchspecs.spec_from_index(builtin, existing_raw, rule=rule)
        if existing_spec is None:
            raise PinPatchError(f"existing index doc is invalid: {target_path}")
        base_spec = existing_spec

    final_version = version if version is not None else base_spec.version
    final_url = url if url is not None else base_spec.url

    archive_sha256, archive_size = _hash_file(zip_path)
    entries: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for item in base_spec.files:
                try:
                    info = zf.getinfo(item.member)
                except KeyError:
                    raise PinPatchError(
                        f"required member {item.member!r} is missing from {zip_path}"
                    ) from None
                if info.is_dir():
                    raise PinPatchError(f"member {item.member!r} is a directory in {zip_path}")
                with zf.open(info, "r") as member_handle:
                    member_sha256, member_size = _hash_stream(member_handle)
                if before_from is not None:
                    before_sha256, before_size = _before_pins_from_install(before_from, item.dest)
                else:
                    before_sha256 = item.before_sha256
                    before_size = item.before_size
                entries.append(
                    {
                        "member": item.member,
                        "dest": item.dest,
                        "sha256": member_sha256,
                        "size": member_size,
                        "before_sha256": before_sha256,
                        "before_size": before_size,
                    }
                )
    except zipfile.BadZipFile as exc:
        raise PinPatchError(f"invalid zip archive {zip_path}: {exc}") from exc

    doc: dict[str, Any] = {
        "schema": 1,
        "id": builtin.id,
        "kind": rule.kind,
        "version": final_version,
        "url": final_url,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "files": entries,
    }
    if patchspecs.spec_from_index(builtin, doc, rule=rule) is None:
        raise PinPatchError("generated index document failed spec_from_index validation")
    return target_path, doc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and validate a Nadare index patch pin document from a local zip."
    )
    parser.add_argument("--id", required=True, dest="patch_id", help="patch id (e.g. kibotl-en)")
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path", help="local zip path")
    parser.add_argument(
        "--index", required=True, type=Path, dest="index_dir", help="index checkout directory"
    )
    parser.add_argument("--version", default=None, help="override patch version string")
    parser.add_argument("--url", default=None, help="override download URL")
    parser.add_argument(
        "--before-from",
        type=Path,
        default=None,
        dest="before_from",
        help="recompute before_sha256/before_size from a pristine game install folder",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        target_path, doc = build_pin_document(
            args.patch_id,
            args.zip_path,
            args.index_dir,
            version=args.version,
            url=args.url,
            before_from=args.before_from,
        )
        atomicfile.write(
            target_path,
            json.dumps(doc, indent=2) + "\n",
            make_parent=True,
            sync_dir=True,
        )
    except (PinPatchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
