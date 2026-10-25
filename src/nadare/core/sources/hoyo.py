# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for hoyo."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

from .. import directdl
from . import Fetch, default_fetch, ungzip

API_URL = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGamePackages"
    "?launcher_id=VYTpXlbWo8"
)

_TITLES: dict[str, tuple[str, str]] = {
    "hk4e_global": ("genshin", "GenshinImpact.exe"),
    "nap_global": ("zzz", "ZenlessZoneZero.exe"),
    "hkrpg_global": ("hsr", "StarRail.exe"),
    "bh3_global": ("hi3", "BH3.exe"),
}


def resolve(
    biz: str, fetch: Fetch | None = None, language: str = "en-us"
) -> directdl.Plan:
    """Return the latest full download plan for ``biz``."""
    data = _load(fetch or default_fetch)
    package = _package(data, biz)
    major = _major(package)
    if biz not in _TITLES:
        raise directdl.DirectError(f"unknown hoyo biz: {biz}")
    game_tag, exe = _TITLES[biz]
    version = _text(major.get("version"), "version")
    game_pkgs = _items(major.get("game_pkgs"), "game_pkgs")
    audio_pkgs = _items(major.get("audio_pkgs"), "audio_pkgs")

    parts = tuple(_remote(pkg, _name(pkg)) for pkg in game_pkgs)
    if not parts:
        raise directdl.DirectError(f"no game packages for {biz}")
    base_name = directdl.safe_relative_path(f"{game_tag}-{version}.pack")
    base = directdl.Archive(
        name=base_name,
        source=directdl.RemoteFile(
            path=base_name,
            url=parts[0].url,
            size=sum(part.size or 0 for part in parts),
        ),
        parts=parts if len(parts) > 1 else (),
        unwrap=_unwrap(parts[0].path),
        stream=True,
    )
    if len(parts) == 1:
        base = directdl.Archive(
            name=base_name,
            source=parts[0],
            unwrap=_unwrap(parts[0].path),
            stream=True,
        )

    archives = [base]
    if audio_pkgs:
        audio_pkg = _audio(audio_pkgs, language)
        audio_name = directdl.safe_relative_path(f"{game_tag}-{version}-audio-{language}")
        archives.append(
            directdl.Archive(
                name=audio_name,
                source=_remote(audio_pkg, audio_name, _name(audio_pkg)),
                unwrap=_unwrap(_name(audio_pkg)),
                stream=True,
            )
        )
    return directdl.Plan(
        game_tag=game_tag,
        version=version,
        archives=tuple(archives),
        exe=exe,
    )


def _unwrap(name: str) -> tuple[str, ...]:
    """The extraction step for a package, from its file name (``.7z``/``.7z.001`` or zip)."""
    lowered = name.lower()
    if lowered.endswith(".7z") or ".7z." in lowered:
        return ("7z",)
    return ("zip",)


def _load(fetch: Fetch) -> Mapping[str, Any]:
    try:
        payload = json.loads(ungzip(fetch(API_URL, None)))
    except (ValueError, TypeError, OSError) as exc:
        raise directdl.DirectError(f"could not read hoyo packages: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise directdl.DirectError("hoyo response is not an object")
    if payload.get("retcode") != 0:
        raise directdl.DirectError(f"hoyo API error: {payload.get('message', 'unknown')}")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise directdl.DirectError("hoyo response has no data")
    return data


def _package(data: Mapping[str, Any], biz: str) -> Mapping[str, Any]:
    packages = _items(data.get("game_packages"), "game_packages")
    for package in packages:
        game = package.get("game") if isinstance(package, Mapping) else None
        if isinstance(game, Mapping) and game.get("biz") == biz:
            return package
    raise directdl.DirectError(f"unknown hoyo biz: {biz}")


def _major(package: Mapping[str, Any]) -> Mapping[str, Any]:
    main = package.get("main")
    major = main.get("major") if isinstance(main, Mapping) else None
    if not isinstance(major, Mapping):
        raise directdl.DirectError("hoyo package has no main build")
    return major


def _items(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(x, Mapping) for x in value):
        raise directdl.DirectError(f"hoyo {label} is not a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise directdl.DirectError(f"hoyo package has no {label}")
    return value


def _name(pkg: Mapping[str, Any]) -> str:
    url = _url(pkg.get("url"))
    name = quote(urlsplit(url).path.rsplit("/", 1)[-1], safe="/")
    return directdl.safe_relative_path(name)


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise directdl.DirectError(f"hoyo URL is not https: {value!r}")
    return value


def _size(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise directdl.DirectError(f"invalid hoyo package size: {value!r}") from exc


def _remote(
    pkg: Mapping[str, Any], path: str, remote_name: str | None = None
) -> directdl.RemoteFile:
    md5 = pkg.get("md5")
    if not isinstance(md5, str) or not md5:
        raise directdl.DirectError("hoyo package has no md5")
    return directdl.RemoteFile(
        path=directdl.safe_relative_path(remote_name or path),
        url=_url(pkg.get("url")),
        size=_size(pkg.get("size")),
        md5=md5,
    )


def _audio(pkgs: list[Mapping[str, Any]], language: str) -> Mapping[str, Any]:
    for pkg in pkgs:
        if pkg.get("language") == language:
            return pkg
    raise directdl.DirectError(f"no hoyo audio package for language {language}")
