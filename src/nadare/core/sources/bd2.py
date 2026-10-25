# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for bd2."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit

from .. import directdl
from . import Fetch, default_fetch

_GAME_URL = "https://pc.bd2.pmang.cloud/browndust2starter/games/10000001"
_GAME_TAG = "bd2"


def _json(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise directdl.DirectError("bd2: response is not JSON") from exc
    if not isinstance(value, dict):
        raise directdl.DirectError("bd2: response is not an object")
    if value.get("result", 0) != 0:
        raise directdl.DirectError(f"bd2: API error {value.get('result')!r}")
    return value


def _https(url: str) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise directdl.DirectError(f"bd2: expected an https URL, got {url!r}")
    return url


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    get = fetch if fetch is not None else default_fetch
    meta = _json(get(_GAME_URL + "/game.json", None))

    base = _https(meta.get("download_url"))
    version = meta.get("last_version")
    exe = meta.get("exe_file")
    last = meta.get("last_file")
    if not isinstance(version, str) or not version:
        raise directdl.DirectError("bd2: missing last_version")
    if not isinstance(exe, str) or not exe:
        raise directdl.DirectError("bd2: missing exe_file")
    if not isinstance(last, dict):
        raise directdl.DirectError("bd2: missing last_file")

    name = last.get("file")
    crc = last.get("tied_crc")
    if not isinstance(name, str) or not name:
        raise directdl.DirectError("bd2: missing last_file.file")
    if not isinstance(crc, str) or not crc:
        raise directdl.DirectError("bd2: missing last_file.tied_crc")

    relative = directdl.safe_relative_path(name.lstrip("/"))
    source = directdl.RemoteFile(
        path=relative,
        url=_https(base.rstrip("/") + "/" + quote(relative, safe="/")),
    )
    archive = directdl.Archive(
        name=relative,
        source=source,
        unwrap=("zip", "zip"),
        inner_crc32=crc,
    )
    return directdl.Plan(
        game_tag=_GAME_TAG,
        version=version,
        archives=(archive,),
        exe=directdl.safe_relative_path(exe),
    )
