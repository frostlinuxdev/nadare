# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Strinova's public archive index and installed-file manifest."""

from __future__ import annotations

import re
from typing import Any

from .. import directdl
from . import Fetch, default_fetch

BASE = "https://resource-download.strinova.com/Client/Win/GameDepot"
MANIFEST_URL = BASE + "/Game_Release/manifest.txt"
EXE = "Strinova.exe"
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_MD5 = re.compile(r"[0-9a-fA-F]{32}")


def _lines(raw: Any) -> list[str]:
    if not isinstance(raw, bytes):
        raise directdl.DirectError("strinova: manifest must be bytes")
    try:
        lines = [line.strip() for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    except UnicodeError as exc:
        raise directdl.DirectError("strinova: manifest is not UTF-8") from exc
    if not lines:
        raise directdl.DirectError("strinova: empty manifest")
    return lines


def _version(value: str) -> str:
    if not _VERSION.fullmatch(value):
        raise directdl.DirectError("strinova: invalid version")
    return value


def _fields(line: str, count: int) -> list[str]:
    fields = line.split("|")
    if len(fields) != count:
        raise directdl.DirectError("strinova: malformed manifest row")
    return fields


def _hash_size(md5: str, size: str) -> tuple[str, int]:
    if not _MD5.fullmatch(md5) or not re.fullmatch(r"[0-9]+", size):
        raise directdl.DirectError("strinova: invalid MD5 or size")
    return md5.lower(), int(size)


def resolve(fetch: Fetch | None = None, *, installed: str | None = None) -> directdl.Plan:
    """Choose one direct update when offered, otherwise every full archive."""
    get = fetch or default_fetch
    target = None
    rows = []
    seen = set()
    lines = iter(_lines(get(MANIFEST_URL, None)))
    for line in lines:
        if line.startswith("--last_version--"):
            if target is not None:
                raise directdl.DirectError("strinova: repeated latest version")
            value = line.removeprefix("--last_version--").strip()
            target = _version(value or next(lines, ""))
            continue
        if line == "--download_version--":
            continue
        hop, path, md5, size = _fields(line, 4)
        versions = hop.split("->")
        if len(versions) != 2:
            raise directdl.DirectError("strinova: malformed update row")
        origin, dest = map(_version, versions)
        if not path.startswith("/Game_Release/"):
            raise directdl.DirectError("strinova: invalid archive path")
        relative = directdl.safe_relative_path(path[1:])
        if not relative.endswith(".7z"):
            raise directdl.DirectError("strinova: expected a 7z archive")
        digest, length = _hash_size(md5, size)
        key = (origin, dest, relative.lower())
        if key in seen:
            raise directdl.DirectError("strinova: repeated archive")
        seen.add(key)
        rows.append(
            (
                origin,
                dest,
                directdl.Archive(
                    name=relative,
                    source=directdl.RemoteFile(relative, BASE + path, length, md5=digest),
                    unwrap=("7z",),
                ),
            )
        )
    if target is None:
        raise directdl.DirectError("strinova: missing latest version")
    full = [arc for origin, dest, arc in rows if origin == dest == target]
    if not full:
        raise directdl.DirectError("strinova: no full archives for latest version")
    updates = [
        arc
        for origin, dest, arc in rows
        if origin == installed and dest == target and origin != dest
    ]
    if len(updates) > 1:
        raise directdl.DirectError("strinova: ambiguous update")

    files = []
    paths = set()
    for line in _lines(get(BASE + f"/Game_Release/{target}/manifest.txt", None)):
        path, md5, size = _fields(line, 3)
        path = directdl.safe_relative_path(path)
        digest, length = _hash_size(md5, size)
        if path.lower() in paths:
            raise directdl.DirectError("strinova: repeated file")
        paths.add(path.lower())
        files.append({"path": path, "md5": digest, "size": length})
    return directdl.Plan(
        "strinova",
        target,
        archives=tuple(updates or full),
        exe=EXE,
        extra={"verification_files": tuple(files), "full_archives": tuple(full)},
    )
