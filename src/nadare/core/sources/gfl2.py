# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for gfl2."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from .. import directdl
from . import Fetch, default_fetch

FLEX_URL = "https://gf2-launcher-us.sunborngame.com/flexible_config"
#: The Haoplay global build publishes the same flexible config on its own host.
HAOPLAY_FLEX_URL = "https://gf2-launcher-intl.haoplay.com/flexible_config"
DATA_DIR = "GF2_Exilium_Data/LocalCache/Data/"
ARCHIVE_NAME = "GF2_Exilium_Origin.zip"


def _loads(data: bytes | str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise directdl.DirectError(f"invalid JSON in {what}") from exc
    if not isinstance(value, dict):
        raise directdl.DirectError(f"unexpected JSON in {what}")
    return value


def _field(obj: dict[str, Any], key: str) -> Any:
    value = obj.get(key)
    if value is None:
        raise directdl.DirectError(f"missing {key!r} in flexible config")
    return value


def _https(url: Any, what: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise directdl.DirectError(f"{what} is not an https URL")
    return url


def _join(base: str, *parts: str) -> str:
    return base.rstrip("/") + "/" + "/".join(part.strip("/") for part in parts)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise directdl.DirectError(f"invalid size: {value!r}") from exc


def resolve(
    fetch: Fetch | None = None,
    *,
    flex_url: str = FLEX_URL,
    game_tag: str = "gfl2",
    client_only: bool = False,
) -> directdl.Plan:
    fetch = fetch or default_fetch
    doc = _loads(fetch(flex_url, None), "flexible config")
    if "code" in doc and str(doc["code"]) != "0":
        raise directdl.DirectError(f"flexible config error: {doc.get('msg', doc['code'])}")
    data = _field(doc, "data")
    if not isinstance(data, dict):
        raise directdl.DirectError("unexpected 'data' in flexible config")
    flexible = _field(data, "flexible_config")
    if not isinstance(flexible, dict):
        raise directdl.DirectError("unexpected 'flexible_config' in flexible config")
    plug_value = _field(flexible, "nexon_plug")
    if isinstance(plug_value, str):
        plug = _loads(plug_value, "nexon_plug")
    elif isinstance(plug_value, dict):
        plug = plug_value
    else:
        raise directdl.DirectError("unexpected 'nexon_plug' in flexible config")

    version = str(_field(plug, "ab_resource_version"))
    client_version = str(_field(plug, "game_client_version"))
    resource_version = version.rsplit(".", 1)[-1]
    cdn = _https(_field(plug, "ab_resource_addr"), "ab_resource_addr")
    download_addr = _https(_field(plug, "game_download_addr"), "game_download_addr")

    version_url = _join(cdn, "AssetBundles_Windows", resource_version, "Version.txt")
    version_data = b"" if client_only else fetch(version_url, None)

    files: list[directdl.RemoteFile] = []
    for line in version_data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or "#" not in line:
            continue
        parts = line.split("#")
        if len(parts) < 4:
            raise directdl.DirectError(f"malformed Version.txt line: {line!r}")
        name, md5, size, ver = parts[0], parts[1], parts[2], parts[3]
        rel = name.removeprefix("{0}/").lstrip("/")
        dest = directdl.safe_relative_path(DATA_DIR + "AssetBundles_Windows/" + rel)
        url = _join(cdn, "AssetBundles_Windows", ver, quote(rel, safe="/"))
        files.append(
            directdl.RemoteFile(path=dest, url=_https(url, "asset URL"), size=_int(size), md5=md5)
        )

    if not client_only:
        version_dest = directdl.safe_relative_path(DATA_DIR + "Version_D.txt")
        files.append(
            directdl.RemoteFile(
                path=version_dest,
                url=version_url,
                size=len(version_data),
                md5=hashlib.md5(version_data).hexdigest(),
            )
        )

    archive = directdl.Archive(
        name=directdl.safe_relative_path(ARCHIVE_NAME),
        source=directdl.RemoteFile(
            path=directdl.safe_relative_path(ARCHIVE_NAME),
            url=_https(_join(download_addr, client_version, ARCHIVE_NAME), "archive URL"),
        ),
        stream=True,
    )

    return directdl.Plan(
        game_tag=game_tag,
        version=(f"{version}+client-{client_version}" if client_only else version),
        files=tuple(files),
        archives=(archive,),
        exe="GF2_Exilium.exe",
    )


def resolve_haoplay(fetch: Fetch | None = None) -> directdl.Plan:
    """The Haoplay global build: only the small client, the game fetches its resources itself."""
    return resolve(fetch, flex_url=HAOPLAY_FLEX_URL, game_tag="gfl2-haoplay", client_only=True)
