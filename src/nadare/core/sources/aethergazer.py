# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for aethergazer."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from .. import directdl
from ..directdl import require_https
from . import Fetch, default_fetch, ungzip

API_URL = (
    "https://us-api.yongshigame.com/game-website-server/pass/sdk/"
    "get_update_version?gameId=1"
)
_MD5 = re.compile(r"([0-9A-Fa-f]{32})\.zip$", re.IGNORECASE)




def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """Return the current Aether Gazer PC client archive."""
    data = ungzip((fetch or default_fetch)(API_URL, None))
    try:
        body = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise directdl.DirectError("invalid update response") from exc
    if not isinstance(body, dict) or str(body.get("errorCode")) != "0":
        raise directdl.DirectError("update API returned an error")
    data = body.get("data")
    info = data.get("gameUpdateInfo") if isinstance(data, dict) else None  # #812 fuzz
    if not isinstance(info, dict):
        raise directdl.DirectError("missing gameUpdateInfo")

    version = str(info.get("currentVersion") or "")
    url = require_https(info.get("downloadUrl"))
    name = info.get("fileName")
    if not isinstance(name, str) or not name:
        name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = directdl.safe_relative_path(quote(name, safe="/"))

    match = _MD5.search(name)
    md5 = match.group(1).lower() if match else None
    if md5 is None:
        raise directdl.DirectError("update file name has no md5")

    source = directdl.RemoteFile(path=name, url=url, md5=md5)
    # The zip is named after the version and its MD5: one "client" across versions (#967).
    archive = directdl.Archive(
        name=name, source=source, unwrap=("zip",), stream=True, slot="client"
    )
    return directdl.Plan(
        game_tag="aethergazer",
        version=version,
        archives=(archive,),
        exe="AetherGazer.exe",
    )
