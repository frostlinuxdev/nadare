# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Duet Night Abyss (Pan Studio / Hero Games, global PC client).

The publisher's launcher downloads the game anonymously ("Direct Mode"): it reads
``VersionManifest.json`` for the current build, then ``HPatchDiffMd5.json`` for that build's
one full diff, ``full_<build>.hdiff`` (HDiffPatch, made from an empty folder), and unpacks it.
Nadare does the same with :mod:`nadare.core.hdiffsf`, so the launcher (whose own update shipped
malware on 2026-03-18) never runs. Captured 2026-11-13 (#1330); every host answers https.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import directdl
from . import Fetch, default_fetch

#: Newest first; the launcher benchmarks these and picks one. Each serves the same tree.
CDNS: tuple[str, ...] = (
    "https://pan01-cdn-ali-jp.dna-panstudio.com/",
    "https://pan01-cdn-aws-jp.dna-panstudio.com/",
    "https://pan01-cdn-eo-jp.dna-panstudio.com/",
    "https://pan01-cdn-hs-jp.dna-panstudio.com/",
)
PACKAGES = "Packages/Global/WindowsNoEditor/PC_OBT_Global_Pub/"
#: The client's own start stub; it starts ``EM/Binaries/Win64/EM-Win64-Shipping.exe``.
EXE = "EM.exe"
GAME_EXE = "EM/Binaries/Win64/EM-Win64-Shipping.exe"
_BUILD = re.compile(r"^[0-9]{1,9}$")
_NUMBER = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$")
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


def _json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise directdl.DirectError(f"dna: {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise directdl.DirectError(f"dna: {what} is not an object")
    return value


def _size(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise directdl.DirectError(f"dna: bad {what}")
    return value


def _plan(get: Fetch, cdn: str) -> directdl.Plan:
    manifest = _json(get(cdn + PACKAGES + "VersionManifest.json", None), "version manifest")
    build, number = manifest.get("latest_version"), manifest.get("latest_version_number")
    if not isinstance(build, str) or not _BUILD.match(build):
        raise directdl.DirectError(f"dna: bad latest_version {build!r}")
    if not isinstance(number, str) or not _NUMBER.match(number):
        raise directdl.DirectError(f"dna: bad latest_version_number {number!r}")
    folder = f"{cdn}{PACKAGES}{number}/{build}/full_{build}/"
    doc = _json(get(folder + "HPatchDiffMd5.json", None), "diff list")
    entry = doc.get("hdiff_file")
    if not isinstance(entry, dict):
        raise directdl.DirectError("dna: the diff list has no hdiff_file")
    name = entry.get("name")
    if name != f"full_{build}.hdiff":
        raise directdl.DirectError(f"dna: unexpected diff name {name!r}")
    md5 = entry.get("md5")
    if not isinstance(md5, str) or not _MD5.match(md5):
        raise directdl.DirectError("dna: bad diff md5")
    size = _size(entry.get("size"), "diff size")
    installed = _size(doc.get("new_size", 0), "installed size")
    source = directdl.RemoteFile(path=name, url=folder + name, size=size, md5=md5.lower())
    archive = directdl.Archive(name=name, source=source, unwrap=("hdiff",), slot="client")
    return directdl.Plan(
        game_tag="dna",
        version=f"{number} ({build})",
        archives=(archive,),
        exe=EXE,
        extra={"installed_size": installed},
    )


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """The current full client, from the first CDN that answers."""
    get = fetch or default_fetch
    errors: list[str] = []
    for cdn in CDNS:
        try:
            return _plan(get, cdn)
        except directdl.DirectError as exc:
            if "HTTP" not in str(exc) and "connect" not in str(exc).lower():
                raise  # a bad document is not a network problem: do not paper over it
            errors.append(str(exc))
    raise directdl.DirectError("dna: no CDN answered: " + "; ".join(errors))
