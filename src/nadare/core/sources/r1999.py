# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Reverse: 1999 global PC packages from the publisher's update service."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from .. import directdl
from . import post_fetch

API = "https://hotupdate-hw.sl916.com/diff-update"
EXE = "reverse1999.exe"
Post = Callable[[str, bytes, Mapping[str, str]], bytes]
_MAX_REPLY = 4 << 20
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_PART = re.compile(r"size_(\d+)_md5_([0-9a-f]{32})_\d+\.7z$")


def _allowed(url: str, host: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == host
            and not parsed.fragment
            and "\\" not in url
            and not any(ord(char) < 33 for char in url)
        )
    except ValueError:
        return False


def download_allowed(url: str) -> bool:
    return _allowed(url, "download.bluepoch.com")


def default_post(url: str, body: bytes, headers: Mapping[str, str]) -> bytes:
    """Send JSON through the shared retry/cancel path, keeping redirects on the API host."""
    # Both /version and /resource are read-only update queries, so full retries are safe.
    return post_fetch(
        url,
        body,
        headers,
        max_bytes=_MAX_REPLY,
        redirect_allowed=lambda target: _allowed(target, "hotupdate-hw.sl916.com"),
        idempotent=True,
    )


def _request(
    post: Post,
    endpoint: str,
    current: str,
    env: int,
    target: str | None = None,
    *,
    null_data: bool = False,
) -> dict | None:
    if not isinstance(current, str) or (current and not _VERSION.fullmatch(current)):
        raise directdl.DirectError("r1999: invalid current version")
    if isinstance(env, bool) or env not in (3, 4):
        raise directdl.DirectError("r1999: invalid environment")
    # the launcher signs each call with md5 of its fixed parameters
    sign = hashlib.md5(f"60001_2_{env}_200_6001_{current}".encode()).hexdigest()
    body = {
        "gameId": "60001",
        "osType": 2,
        "envType": env,
        "channelId": 200,
        "subChannelId": "6001",
        "currentVersion": current,
        "sign": sign,
    }
    if target is not None:
        body["targetVersion"] = target
    raw = post(
        API + endpoint,
        json.dumps(body).encode(),
        {"lang": "en", "Content-Type": "application/json"},
    )
    try:
        document = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise directdl.DirectError("r1999: update response is not JSON") from exc
    if not isinstance(document, dict) or document.get("code") != 200:
        raise directdl.DirectError("r1999: invalid update response")
    data = document.get("data")
    if data is None and null_data:
        # The live service answers a client already on the latest version with "data": null.
        return None
    if not isinstance(data, dict):
        raise directdl.DirectError("r1999: invalid update response")
    return data


def latest_version(
    post: Post | None = None, *, current_version: str = "", env_type: int = 4
) -> str:
    data = _request(post or default_post, "/version", current_version, env_type)
    version = data.get("latestVersion")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise directdl.DirectError("r1999: invalid latest version")
    return version


def _size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise directdl.DirectError("r1999: invalid package size")
    return value


def _nothing_to_do(version: str) -> directdl.Plan:
    """The plan for an install already on the latest version: fetch nothing."""
    return directdl.Plan("r1999", version, exe=EXE, redirect_allowed=download_allowed)


def resolve(
    post: Post | None = None, *, current_version: str = "", env_type: int = 4
) -> directdl.Plan:
    post = post or default_post
    version = latest_version(post, current_version=current_version, env_type=env_type)
    if current_version and directdl.versions_equal(current_version, version):
        # Nothing to do: do not ask for a package the service would answer with null.
        return _nothing_to_do(version)
    data = _request(
        post, "/resource", current_version, env_type, version, null_data=True
    )
    if data is None:
        return _nothing_to_do(version)
    package = data.get("diffPackage") if current_version else None
    delta = package is not None
    if package is None:
        package = data.get("fullPackage")
    if not isinstance(package, dict):
        raise directdl.DirectError("r1999: missing package")
    _size(package.get("packageSize"))  # only approximate; the parts' sizes are exact
    installed = _size(package.get("packageRealSize"))
    urls = package.get("packageUrls")
    if not isinstance(urls, list) or not urls:
        raise directdl.DirectError("r1999: missing package URLs")
    # packageMd5 is not a checksum; the filenames carry each archive's metadata.
    archives = []
    for index, url in enumerate(urls):
        if not isinstance(url, str) or not download_allowed(url):
            raise directdl.DirectError("r1999: refused package URL")
        directdl.safe_relative_path(unquote(urlsplit(url).path).lstrip("/"))
        metadata = _PART.search(urlsplit(url).path)
        if metadata is None:
            raise directdl.DirectError("r1999: missing archive size or MD5")
        name = f"package-{index:02}.7z"
        if delta:
            name = f"delta-{current_version}-to-{version}-{name}"
        source = directdl.RemoteFile(
            name,
            url,
            size=int(metadata[1]),
            md5=metadata[2],
        )
        # Each URL is a standalone archive.
        # Use the disk path so the download host guard also applies to redirects.
        archives.append(
            directdl.Archive(name, source, unwrap=("7z",), slot="client", additive=delta)
        )
    size = sum(archive.source.size for archive in archives)
    return directdl.Plan(
        "r1999",
        version,
        archives=tuple(archives),
        exe=EXE,
        extra={"installed_size": installed, "download_size": size},
        redirect_allowed=download_allowed,
    )
