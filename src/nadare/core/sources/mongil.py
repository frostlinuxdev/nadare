# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for mongil."""

from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from .. import directdl
from . import Fetch, default_fetch

_URL = "https://apis.netmarble.com/cpplauncher/api/game/monster2/builds?buildCode=A"
_HEADERS = {
    "X-NM-LAUNCHER-CH": "ypWjRL2aNi",
    "X-NM-LAUNCHER-LANG": "en",
}
_GAME_TAG = "mongil"
_EXE = "STARDIVE.exe"


def _load(fetch: Fetch) -> dict:
    try:
        payload = json.loads(fetch(_URL, dict(_HEADERS)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise directdl.DirectError("mongil: invalid JSON response") from exc
    if not isinstance(payload, dict):
        raise directdl.DirectError("mongil: unexpected response body")
    if payload.get("code") not in (0, "0"):
        raise directdl.DirectError(f"mongil: API error: {payload.get('msg', payload.get('code'))}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise directdl.DirectError("mongil: missing data")
    return data


def _https(url: object) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise directdl.DirectError(f"mongil: not an https URL: {url!r}")
    return url


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    fetch = fetch or default_fetch
    data = _load(fetch)

    version = data.get("buildVersion")
    if not isinstance(version, str) or not version:
        raise directdl.DirectError("mongil: missing buildVersion")
    exe = data.get("executeFileName") or _EXE
    if not isinstance(exe, str):
        raise directdl.DirectError("mongil: invalid executeFileName")

    url = _https(data.get("buildDownloadUrl"))
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if not name:
        raise directdl.DirectError("mongil: download URL has no file name")
    name = quote(directdl.safe_relative_path(name), safe="/")

    try:
        size = int(data["buildFileSizeByte"])
    except (KeyError, TypeError, ValueError) as exc:
        raise directdl.DirectError("mongil: invalid buildFileSizeByte") from exc

    md5 = data.get("buildFileHash")
    if not isinstance(md5, str) or not md5:
        raise directdl.DirectError("mongil: missing buildFileHash")

    source = directdl.RemoteFile(
        path=name,
        url=url,
        size=size,
        md5=md5,
        headers=tuple(_HEADERS.items()),
    )
    archive = directdl.Archive(name=name, source=source, unwrap=("zip",), stream=True)
    return directdl.Plan(
        game_tag=_GAME_TAG,
        version=version,
        archives=(archive,),
        exe=directdl.safe_relative_path(exe),
    )
