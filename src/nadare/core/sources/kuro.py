# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Shared source for games on Kuro's launcher platform (Wuthering Waves, Punishing: Gray Raven).

Both publish ``launcher/game/<gameId>/<appId>_<token>/index.json``: the CDN roots plus
the current ``indexFile``, a per-file list with md5 and size. Files are plain objects, so
the plan is an ordinary file list and ``directdl`` checks and downloads it like any other.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlparse

from .. import directdl, kurotokens
from . import Fetch, default_fetch, ungzip

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,64}")


def _json(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(ungzip(data))
    except (ValueError, OSError) as exc:
        raise directdl.DirectError(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise directdl.DirectError(f"{what} is not a JSON object")
    return value


def _https(url: str) -> str:
    if urlparse(url).scheme != "https":
        raise directdl.DirectError(f"refusing a non-https URL: {url!r}")
    return url


def _cdn_root(index: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        default = index["default"]
        cdns = default["cdnList"]
        config = default["config"]
        root = max(cdns, key=lambda cdn: int(cdn["P"]))["url"]
    except (KeyError, TypeError, ValueError) as exc:
        raise directdl.DirectError("index is missing the CDN list or config") from exc
    if not isinstance(config, dict):
        raise directdl.DirectError("index config is not an object")
    return config, _https(str(root))


def _index_url(group: str, prefix: str, token: str) -> str:
    if not _TOKEN_RE.fullmatch(token or ""):
        raise directdl.DirectError("the Kuro token must be letters and digits only")
    return (
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/"
        f"{group}/{prefix}_{token}/index.json"
    )


def resolve_with_tokens(
    *,
    game_tag: str,
    group: str,
    prefix: str,
    builtin: str,
    token: str | None,
    exe: str,
    fetch: Fetch | None,
    refused_hint: str,
) -> directdl.Plan:
    """Plan for a Kuro game, trying the profile token, then published, then builtin.

    An explicit ``token`` is the only candidate. The published token is only
    consulted in the real runtime (``fetch`` is ``None``); an injected fetch is
    a test and stays offline.
    """
    if token:
        candidates = [token]
    elif fetch is None:
        candidates = []
        published = kurotokens.published(game_tag)
        if published:
            candidates.append(published)
        if builtin not in candidates:
            candidates.append(builtin)
    else:
        candidates = [builtin]

    last: directdl.DirectError | None = None
    for candidate in candidates:
        url = _index_url(group, prefix, candidate)
        try:
            return resolve_game(
                url,
                game_tag=game_tag,
                exe=exe,
                fetch=fetch,
                refused_hint=refused_hint,
            )
        except directdl.DirectError as exc:
            last = exc
    if fetch is None and not token:
        # Every known token was refused: the index may have rotated it within the cache's
        # lifetime, so ask it once more, ignoring the cache.
        fresh = kurotokens.published(game_tag, refresh=True)
        if fresh and fresh not in candidates:
            try:
                return resolve_game(
                    _index_url(group, prefix, fresh),
                    game_tag=game_tag,
                    exe=exe,
                    fetch=fetch,
                    refused_hint=refused_hint,
                )
            except directdl.DirectError as exc:
                last = exc
    if last is None:  # pragma: no cover - candidates is never empty
        raise directdl.DirectError(refused_hint)
    raise last


def resolve_game(
    index_url: str,
    *,
    game_tag: str,
    exe: str,
    fetch: Fetch | None = None,
    refused_hint: str = "",
) -> directdl.Plan:
    """Plan for a Kuro launcher-platform game from its ``index.json`` URL.

    The index names the CDN roots and the current ``indexFile``, a list of
    ``{dest, md5, size}``; every file is a plain object under ``baseUrl``. A refused
    index raises :class:`directdl.DirectError` carrying ``refused_hint``.
    """
    fetch = fetch or default_fetch
    try:
        raw = fetch(index_url, None)
    except directdl.DirectError as exc:
        raise directdl.DirectError(f"{exc}. {refused_hint}".strip()) from exc
    index = _json(raw, "index")
    config, root = _cdn_root(index)

    try:
        version = str(config["version"])
        base_url = str(config["baseUrl"])
        index_file = str(config["indexFile"])
    except KeyError as exc:
        raise directdl.DirectError(f"index config is missing {exc.args[0]!r}") from exc

    list_url = _https(root + quote(index_file, safe="/"))
    manifest = _json(fetch(list_url, None), "indexFile")

    resources = manifest.get("resource")
    if not isinstance(resources, list):
        raise directdl.DirectError("indexFile is missing its resource list")

    files: list[directdl.RemoteFile] = []
    for item in resources:
        if not isinstance(item, dict):
            raise directdl.DirectError("indexFile resource entry is not an object")
        try:
            path = directdl.safe_relative_path(item["dest"])
            md5 = str(item["md5"])
            size = int(item["size"])
        except KeyError as exc:
            raise directdl.DirectError(f"resource is missing {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise directdl.DirectError("resource has an invalid size") from exc
        url = _https(root + quote(base_url + path, safe="/"))
        files.append(directdl.RemoteFile(path=path, url=url, size=size, md5=md5))

    return directdl.Plan(
        game_tag=game_tag,
        version=version,
        files=tuple(files),
        exe=exe,
    )
