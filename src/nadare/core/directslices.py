# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Reuse an unchanged, hashed slice object across manifest versions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .directdl import Archive


def matches(
    archive: Archive,
    record: dict[str, Any],
    dest: Path,
    *,
    hashes: bool = True,
) -> bool:
    """A version change alone need not fetch the same verified Pak again.

    The caller still checks the recorded object URL and hashes. Also check the new
    manifest's member paths, sizes and hashes, so a changed mapping or damaged file
    cannot reuse the old record. ``hashes=False`` compares sizes only, for a caller
    that already hashed the install.
    """
    from . import directdl as dd

    if archive.unwrap != ("slices",) or not archive.source.md5 or not archive.members:
        return False
    members = record.get("members")
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        return False
    try:
        recorded = {dd.safe_relative_path(m) for m in members}
        current = {dd.safe_relative_path(m.path) for m in archive.members}
    except dd.DirectError:
        return False
    if recorded != current:
        return False
    return all(
        m.md5
        and dd._verify_remote(
            dest / dd.safe_relative_path(m.path),
            dd.RemoteFile(m.path, "", m.size, md5=m.md5),
            hashes=hashes,
        )
        for m in archive.members
    )
