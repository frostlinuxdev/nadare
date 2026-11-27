# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Tower of Fantasy's public Windows file list and content-addressed objects."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PureWindowsPath

from .. import directdl
from . import Fetch, default_fetch, nte

ROOT = "https://htkrydpatch1.wmupd.com/clientRes/OBPC_2_5"
CONFIG_URL = ROOT + "/Version/Windows/config.xml"
EXE = "Client/WindowsNoEditor/Hotta/Binaries/Win64/QRSL.exe"


def plan_from_xml(xml: str, version: str, installed_size: int) -> directdl.Plan:
    """Reuse the patcher file and Pak parser; check the shape, not the published total.

    ``installed_size`` is ``config.xml``'s ``ResSize``. Publisher totals are approximate
    (the same patcher format was one file short on R1999), so a difference is not a reason
    to refuse an otherwise valid manifest: the plan's own sum is the install size hint.
    """
    plan = replace(nte.plan_from_xml(xml, ROOT, version), game_tag="tof", exe=EXE)
    paths: set[str] = set()
    total = 0
    for item in (*plan.files, *(m for a in plan.archives for m in a.members)):
        if PureWindowsPath(item.path).drive:
            raise directdl.DirectError(f"install path has a drive: {item.path}")
        key = item.path.casefold()  # Windows paths: one file whatever the case
        if key in paths:
            raise directdl.DirectError(f"duplicate install path: {item.path}")
        paths.add(key)
        total += item.size
    pak_names: set[str] = set()
    for archive in plan.archives:
        if archive.name.casefold() in pak_names:
            raise directdl.DirectError(f"duplicate Pak: {archive.name}")
        pak_names.add(archive.name.casefold())
        for member in archive.members:
            if member.offset + member.size > archive.source.size:
                raise directdl.DirectError(f"Pak entry exceeds its object: {member.path}")
    if not paths:
        raise directdl.DirectError("the manifest lists no files")
    if EXE.casefold() not in paths:
        raise directdl.DirectError(f"the manifest does not install {EXE}")
    return replace(plan, extra={**plan.extra, "installed_size": total})


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """Fetch the current full manifest, ignoring patch lists and lastdiff."""
    fetch = fetch or default_fetch
    config = nte._xml(fetch(CONFIG_URL, None), "Tower of Fantasy config")
    version = nte._text(config, "ResVersion")
    if not nte._VERSION.fullmatch(version):
        raise directdl.DirectError("config has an invalid ResVersion")
    installed_size = nte._int(nte._text(config, "ResSize"), "ResSize")
    data = fetch(f"{ROOT}/Version/Windows/version/{version}/ResList.bin.zip", None)
    if not isinstance(data, bytes):
        raise directdl.DirectError("ResList.bin.zip is not bytes")
    container = nte._manifest_zip(data, "ResList.bin.zip")
    return plan_from_xml(nte.decode_container(container, "1000129"), version, installed_size)
