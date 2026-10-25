# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for endfield."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .. import directdl
from . import Fetch, default_fetch, ungzip

API = (
    "https://launcher.gryphline.com/api/game/get_latest"
    "?appcode=YDUTE5gscDZ229CW&channel=6&sub_channel=6"
)


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise directdl.DirectError(f"{what} is not an object")
    return value


def _https(url: Any, what: str) -> str:
    if not isinstance(url, str) or not url:
        raise directdl.DirectError(f"{what} is missing")
    if urlsplit(url).scheme != "https":
        raise directdl.DirectError(f"{what} is not https")
    return url


def _size(value: Any, what: str) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise directdl.DirectError(f"{what} is not a size") from None
    if size < 0:
        raise directdl.DirectError(f"{what} is negative")
    return size


def _basename(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if not name:
        raise directdl.DirectError("pack url has no file name")
    return directdl.safe_relative_path(quote(unquote(name), safe=""))


def _archive_name(first_url: str) -> str:
    name = _basename(first_url)
    stem, dot, suffix = name.rpartition(".")
    if dot and suffix.isdigit():
        name = stem
    return directdl.safe_relative_path(name)


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    data = ungzip((fetch or default_fetch)(API, None))
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise directdl.DirectError("endfield API returned invalid JSON") from None

    root = _mapping(payload, "endfield API response")
    if root.get("action") != 1:
        raise directdl.DirectError(f"endfield API error: {root.get('action')!r}")
    version = root.get("version")
    if not isinstance(version, str) or not version:
        raise directdl.DirectError("endfield API response has no version")

    pkg = _mapping(root.get("pkg"), "pkg")
    raw_packs = pkg.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise directdl.DirectError("endfield API response has no packs")

    parts: list[directdl.RemoteFile] = []
    total = 0
    for index, raw in enumerate(raw_packs, start=1):
        pack = _mapping(raw, f"pack {index}")
        url = _https(pack.get("url"), f"pack {index} url")
        size = _size(pack.get("package_size"), f"pack {index} size")
        md5 = pack.get("md5")
        if not isinstance(md5, str) or not md5:
            raise directdl.DirectError(f"pack {index} has no md5")
        parts.append(
            directdl.RemoteFile(
                path=_basename(url),
                url=url,
                size=size,
                md5=md5,
            )
        )
        total += size

    name = _archive_name(parts[0].url)
    source = directdl.RemoteFile(
        path=name,
        url=parts[0].url,
        size=total,
    )
    return directdl.Plan(
        game_tag="endfield",
        version=version,
        archives=(directdl.Archive(name=name, source=source, parts=tuple(parts), stream=True),),
        exe="Endfield.exe",
    )
