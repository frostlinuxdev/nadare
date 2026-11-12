# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""NIKKE's launcher on the Jupiter launcher CDN.

The launcher is published anonymously as a rail_setup repository. Nadare asks the
public GetVersion API for the latest launcher build, downloads and verifies every
file itself, and runs no publisher installer. See #949.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from .. import download, railrepo

API_URL = "https://www.jupiterlauncher.com/api/v1/fleet.repo.game.RepoSVC/GetVersion"
REFERER = "https://www.jupiterlauncher.com/"
#: The official miniloader: the fallback installer when the direct launcher download fails.
MINILOADER_URL = "https://nikke-en.com/nikkeminiloader.exe"
CDN_HOST = "cloud.nikke-kr.com"
GAME_ID = 16601
LAUNCHER_GAME_ID = "16701"
BRANCH = "1"
MAX_API_BYTES = 4 << 20
MAX_OBJECT_BYTES = 32 << 20
#: Pause after a failed object attempt; three attempts at most, so two delays.
RETRY_DELAYS = (1.0, 2.0)

Post = Callable[[str, bytes, Mapping[str, str]], bytes]
Fetch = Callable[[str, int, str], bytes]


def _retry_sleep(seconds: float) -> None:
    """The retry pause, kept separate so tests can replace it without slowing down."""
    time.sleep(seconds)


def _url_allowed(url: str, host: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        # An explicit port other than HTTPS's own is not the publisher's service (#1207 b114).
        return (
            parsed.scheme == "https"
            and parsed.hostname == host
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def cdn_url_allowed(url: str) -> bool:
    """Whether ``url`` is HTTPS on the publisher's launcher CDN and nowhere else."""
    return _url_allowed(url, CDN_HOST)


def _api_url_allowed(url: str) -> bool:
    return _url_allowed(url, urllib.parse.urlparse(API_URL).hostname or "")


def _final_url(response: Any) -> str | None:
    """The URL a response finally came from, when the response exposes it."""
    url = getattr(response, "url", None)
    if not url:
        geturl = getattr(response, "geturl", None)
        if callable(geturl):
            try:
                url = geturl()
            except Exception:  # noqa: BLE001 - a missing final URL is not fatal here
                url = None
    return url if isinstance(url, str) else None


def default_post(url: str, body: bytes, headers: Mapping[str, str]) -> bytes:
    """POST ``body`` to the GetVersion API with the shared HTTP stack."""
    try:
        with download.http_open(
            url, headers=headers, data=body, redirect_allowed=_api_url_allowed
        ) as response:
            data = response.read(MAX_API_BYTES + 1)
    except download.DownloadError as exc:
        raise railrepo.RailError(str(exc)) from exc
    if len(data) > MAX_API_BYTES:
        raise railrepo.RailError(f"GetVersion reply is larger than {MAX_API_BYTES} bytes")
    return data


def _document(post: Post | None, need_launcher: int) -> dict[str, Any]:
    """POST the GetVersion request and return the parsed document."""
    post = post or default_post
    body = json.dumps(
        {
            "game_id": GAME_ID,
            "branch_id": 1,
            "version_id": "0",
            "channel_id": 1,
            "need_launcher": need_launcher,
        }
    ).encode()
    headers = {"Content-Type": "application/json", "Referer": REFERER}
    try:
        data = post(API_URL, body, headers)
    except railrepo.RailError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the source contract
        raise railrepo.RailError(f"cannot query the launcher API: {exc}") from exc
    try:
        document = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise railrepo.RailError("the launcher API did not return JSON") from exc
    if not isinstance(document, dict):
        raise railrepo.RailError("the launcher API returned an unexpected document")
    result = document.get("result")
    error_code = result.get("error_code") if isinstance(result, dict) else None
    if error_code != 0:
        raise railrepo.RailError(f"the launcher API returned error {error_code!r}")
    return document


def launcher_info(post: Post | None = None) -> dict[str, Any]:
    """Ask the public API for the global NIKKE launcher record."""
    document = _document(post, 1)
    info = document.get("launcher_version_info")
    if not isinstance(info, dict):
        raise railrepo.RailError("the launcher API returned no launcher record")
    if info.get("game_id") != LAUNCHER_GAME_ID:
        raise railrepo.RailError(
            f"the launcher API returned game {info.get('game_id')!r}, expected {LAUNCHER_GAME_ID}"
        )
    if info.get("branch_id") != BRANCH:
        raise railrepo.RailError(
            f"the launcher API returned branch {info.get('branch_id')!r}, expected {BRANCH}"
        )
    return info


def client_info(post: Post | None = None) -> dict[str, Any]:
    """Ask the public API for the global NIKKE game-client record.

    The client is in ``version_info`` of the same reply as the launcher. Older API
    replies only include it when ``need_launcher`` is 0, so retry once that way.
    """
    document = _document(post, 1)
    info = document.get("version_info")
    if not isinstance(info, dict) or not info:  # an empty record retries too (#1207 b115)
        document = _document(post, 0)
        info = document.get("version_info")
    if not isinstance(info, dict):
        raise railrepo.RailError("the launcher API returned no game client record")
    if info.get("game_id") != str(GAME_ID):
        raise railrepo.RailError(
            f"the launcher API returned game {info.get('game_id')!r}, expected {GAME_ID}"
        )
    if info.get("branch_id") != BRANCH:
        raise railrepo.RailError(
            f"the launcher API returned branch {info.get('branch_id')!r}, expected {BRANCH}"
        )
    return info


def _cdn_repositories(info: dict[str, Any]) -> list[railrepo.Repository]:
    """Repositories accepted only from the publisher's own CDN."""
    repositories = railrepo.repositories(info)
    for repository in repositories:
        if not cdn_url_allowed(repository.cdn_root):
            raise railrepo.RailError(f"repository CDN is not allowed: {repository.cdn_root}")
        if not cdn_url_allowed(repository.manifest_url):
            raise railrepo.RailError(f"manifest URL is not allowed: {repository.manifest_url}")
    return repositories


def launcher_repositories(info: dict[str, Any]) -> list[railrepo.Repository]:
    """The launcher repositories, accepted only from the publisher's own CDN."""
    return _cdn_repositories(info)


def game_repositories(info: dict[str, Any]) -> list[railrepo.Repository]:
    """The game-client repositories, accepted only from the publisher's own CDN."""
    return _cdn_repositories(info)


def fetch_object(url: str, size: int, md5_hex: str) -> bytes:
    """Fetch and verify one CDN object, retrying transient failures a few times."""
    if not cdn_url_allowed(url):
        raise railrepo.RailError(f"refusing to download from {url}")
    if not 0 <= size <= MAX_OBJECT_BYTES:
        raise railrepo.RailError(f"{url} is larger than {MAX_OBJECT_BYTES} bytes")
    last_error = "unknown error"
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with download.http_open(url, redirect_allowed=cdn_url_allowed) as response:
                final_url = _final_url(response)
                data = response.read(size + 1)
        except (download.DownloadError, OSError, http.client.HTTPException) as exc:
            last_error = str(exc) or type(exc).__name__  # IncompleteRead: a cut connection
        else:
            if final_url is not None and not cdn_url_allowed(final_url):
                raise railrepo.RailError(f"redirected to a disallowed URL: {final_url}")
            if len(data) != size:
                last_error = f"size {len(data)} does not match {size}"
            elif hashlib.md5(data).hexdigest() != md5_hex.lower():  # noqa: S324
                last_error = "MD5 does not match the manifest"
            else:
                return data
        if attempt < len(RETRY_DELAYS):
            _retry_sleep(RETRY_DELAYS[attempt])
    raise railrepo.RailError(f"cannot download {url}: {last_error}")


def install_launcher(
    dest: Any,
    *,
    emit: Callable | None = None,
    cancel: Any = None,
    post: Post | None = None,
    fetch: Fetch | None = None,
) -> railrepo.InstallResult:
    """Install the launcher files below ``dest`` without running anything."""
    info = launcher_info(post)
    repositories = launcher_repositories(info)
    return railrepo.install(
        repositories,
        dest,
        fetch=fetch or fetch_object,
        emit=emit,
        cancel=cancel,
    )


#: Launcher files that say whether its sign-in service is current (#1245): the launcher, and the
#: ``intl_service`` program and its two core libraries the launcher-free sign-in runs.
LAUNCHER_KEY_FILES = (
    "nikke_launcher.exe",
    "intl_service/intl_service.exe",
    "intl_service/INTLCore.dll",
    "intl_service/intl_service_core.dll",
)


def launcher_key_entries(
    info: dict[str, Any], *, fetch: Fetch | None = None
) -> dict[str, railrepo.Entry]:
    """The launcher manifest's entries (size, MD5) for :data:`LAUNCHER_KEY_FILES`."""
    wanted = {key.lower(): key for key in LAUNCHER_KEY_FILES}
    found: dict[str, railrepo.Entry] = {}
    for repo in launcher_repositories(info):
        data = railrepo._fetch_verified(
            fetch or fetch_object, repo.manifest_url, repo.manifest_size, repo.manifest_md5
        )
        for entry in railrepo.parse_manifest(data, repo):
            key = wanted.get(entry.path.replace("\\", "/").lower())
            if key and not entry.is_dir:
                found[key] = entry
    missing = [key for key in LAUNCHER_KEY_FILES if key not in found]
    if missing:
        raise railrepo.RailError(f"the launcher manifest has no {', '.join(missing)}")
    return found


#: The game program, as the client manifest names it (``\\`` separators there).
CLIENT_EXE = "NIKKE/game/nikke.exe"


def client_exe_entry(info: dict[str, Any], *, fetch: Fetch | None = None) -> railrepo.Entry:
    """The client manifest's entry for ``nikke.exe`` (its size and MD5), from ``client_info``.

    It says whether an installed client is current. The reply's ``key_file_info`` CRC is not a
    plain CRC32 of the file (measured 2026-11-10), so the manifest is used instead.
    """
    for repo in game_repositories(info):
        data = railrepo._fetch_verified(
            fetch or fetch_object, repo.manifest_url, repo.manifest_size, repo.manifest_md5
        )
        for entry in railrepo.parse_manifest(data, repo):
            if not entry.is_dir and entry.path.replace("\\", "/").lower() == CLIENT_EXE.lower():
                return entry
    raise railrepo.RailError(f"the client manifest has no {CLIENT_EXE}")


def install_game(
    dest: Any,
    *,
    emit: Callable | None = None,
    cancel: Any = None,
    post: Post | None = None,
    fetch: Fetch | None = None,
) -> railrepo.InstallResult:
    """Install the game-client files below ``dest`` without running anything."""
    info = client_info(post)
    repositories = game_repositories(info)
    return railrepo.install(
        repositories,
        dest,
        fetch=fetch or fetch_object,
        emit=emit,
        cancel=cancel,
    )
