# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for aethergazer."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

from .. import directdl
from . import Fetch, default_fetch, ungzip

API_URL = (
    "https://us-api.yongshigame.com/game-website-server/pass/sdk/"
    "get_update_version?gameId=1"
)
_MD5 = re.compile(r"([0-9A-Fa-f]{32})\.zip$", re.IGNORECASE)


def _https(url: Any) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise directdl.DirectError("update URL must be https")
    return url


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """Return the current Aether Gazer PC client archive."""
    data = ungzip((fetch or default_fetch)(API_URL, None))
    try:
        body = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise directdl.DirectError("invalid update response") from exc
    if not isinstance(body, dict) or str(body.get("errorCode")) != "0":
        raise directdl.DirectError("update API returned an error")
    info = (body.get("data") or {}).get("gameUpdateInfo")
    if not isinstance(info, dict):
        raise directdl.DirectError("missing gameUpdateInfo")

    version = str(info.get("currentVersion") or "")
    url = _https(info.get("downloadUrl"))
    name = info.get("fileName")
    if not isinstance(name, str) or not name:
        name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = directdl.safe_relative_path(quote(name, safe="/"))

    match = _MD5.search(name)
    md5 = match.group(1).lower() if match else None
    if md5 is None:
        raise directdl.DirectError("update file name has no md5")

    source = directdl.RemoteFile(path=name, url=url, md5=md5)
    archive = directdl.Archive(name=name, source=source, unwrap=("zip",), stream=True)
    return directdl.Plan(
        game_tag="aethergazer",
        version=version,
        archives=(archive,),
        exe="AetherGazer.exe",
    )
