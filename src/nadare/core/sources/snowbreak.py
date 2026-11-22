# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Snowbreak: Containment Zone (Seasun, global PC client).

Seasun publishes one public manifest per release line: every game file with its MD5 and
size, and each file is stored under ``<pathOffset>/<md5>`` next to the manifest. No sign-in
and no launcher are involved (the community launchers install the same way). The game signs
in inside the client, through the Seasun SDK it ships.

The manifest lives in a *branch* folder that changes with each major release line (``DLC7``,
then ``6e54…`` for 2.x, then ``c34e…`` for 3.x). Seasun's launcher carries the current branch in
its embedded config (``"branch"``), so the list below is newest first and each one is tried in
turn; a branch Seasun has removed answers 403/404 and the next is tried.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import directdl
from . import Fetch, default_fetch

CDN = "https://snowbreak-dl.amazingseasuncdn.com/"
#: Newest first. From launcher 3.6.0's config (``branch``), 2026-11-16.
BRANCHES: tuple[str, ...] = ("c34e2650e07e397c7e55e46b3266572c",)
_BRANCH = re.compile(r"^[0-9a-f]{32}$")


def base_of(branch: str) -> str:
    """The folder that holds ``manifest.json`` and the file objects for ``branch``."""
    if not _BRANCH.match(branch):
        raise directdl.DirectError(f"snowbreak: bad branch {branch!r}")
    return f"{CDN}{branch}/PC/updates/"


EXE = "Game/Binaries/Win64/Game.exe"

#: Parallel downloads. Seasun's CDN caps each connection near 1.6 MB/s and adds them up
#: (2026-11-18: 8 connections 12.8 MB/s, 32 connections 48 MB/s), so the file-count default
#: (16 for Snowbreak's ~1,700 files) left half of a fast line unused (#1611).
WORKERS = 32
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


def _size(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise directdl.DirectError(f"snowbreak: bad size for {name!r}")
    return value


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """Return every file of the current Snowbreak build (first branch that answers)."""
    get = fetch or default_fetch
    errors: list[str] = []
    for branch in BRANCHES:
        base = base_of(branch)
        try:
            raw = get(base + "manifest.json", None)
        except directdl.DirectError as exc:
            errors.append(str(exc))
            continue
        return _plan(raw, base)
    raise directdl.DirectError(
        "snowbreak: no known download branch answers (Seasun may have started a new one): "
        + "; ".join(errors)
    )


def _plan(raw: bytes, base: str) -> directdl.Plan:
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise directdl.DirectError("snowbreak: manifest is not JSON") from exc
    if not isinstance(manifest, dict):
        raise directdl.DirectError("snowbreak: manifest is not an object")
    version = manifest.get("version")
    paks = manifest.get("paks")
    if not isinstance(version, str) or not version:
        raise directdl.DirectError("snowbreak: manifest has no version")
    if not isinstance(paks, list) or not paks:
        raise directdl.DirectError("snowbreak: manifest lists no files")
    offset = directdl.safe_relative_path(manifest.get("pathOffset"))

    files: list[directdl.RemoteFile] = []
    seen: set[str] = set()
    for pak in paks:
        if not isinstance(pak, dict):
            raise directdl.DirectError("snowbreak: a manifest entry is not an object")
        path = directdl.safe_relative_path(pak.get("name"))
        md5 = pak.get("hash")
        if not isinstance(md5, str) or not _MD5.match(md5):
            raise directdl.DirectError(f"snowbreak: bad MD5 for {path!r}")
        size = _size(pak.get("sizeInBytes"), path)
        key = path.lower()  # Windows paths: one file whatever the case
        if key in seen:
            raise directdl.DirectError(f"snowbreak: {path!r} is listed twice")
        seen.add(key)
        files.append(
            directdl.RemoteFile(
                path=path, url=base + offset + "/" + md5.lower(), size=size, md5=md5.lower()
            )
        )
    if EXE.lower() not in seen:
        raise directdl.DirectError(f"snowbreak: the manifest has no {EXE}")
    return directdl.Plan(
        game_tag="snowbreak",
        version=version,
        files=tuple(files),
        exe=EXE,
        extra={"workers": WORKERS},
    )
