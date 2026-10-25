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

#: Games that ship as Sophon chunk manifests, keyed by their branch API game id.
_SOPHON_GAME_IDS: dict[str, str] = {
    "hk4e_global": "gopR6Cufr3",
    "nap_global": "U5hbdsT9W7",
    "hkrpg_global": "4ziysqXOQ8",
}

_BRANCHES_URL = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
    "?launcher_id=VYTpXlbWo8&game_ids%5B%5D="
)
_BUILD_URL = (
    "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild"
    "?branch=main&package_id={package_id}&password={password}"
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
    if biz in _SOPHON_GAME_IDS:
        return _sophon_plan(biz, fetch or default_fetch, language)
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


def _sophon_plan(biz: str, fetch: Fetch, language: str) -> directdl.Plan:
    """Build a plan from the live Sophon chunk manifests for ``biz``."""
    game_tag, exe = _TITLES[biz]
    branch = _load_from(
        _BRANCHES_URL + _SOPHON_GAME_IDS[biz], fetch, "hoyo branches"
    )
    branches = _items(branch.get("game_branches"), "game_branches")
    if not branches:
        raise directdl.DirectError(f"no hoyo branch for {biz}")
    main = branches[0].get("main") if isinstance(branches[0], Mapping) else None
    if not isinstance(main, Mapping):
        raise directdl.DirectError(f"hoyo branch for {biz} has no main build")
    version = _text(main.get("tag"), "version")
    package_id = _text(main.get("package_id"), "package_id")
    password = _text(main.get("password"), "password")

    build = _load_from(
        _BUILD_URL.format(package_id=package_id, password=password), fetch, "hoyo builds"
    )
    manifests = _items(build.get("manifests"), "manifests")
    game_entry: Mapping[str, Any] | None = None
    audio_entry: Mapping[str, Any] | None = None
    for entry in manifests:
        field = entry.get("matching_field")
        if field == "game" and game_entry is None:
            game_entry = entry
        elif field == language and audio_entry is None:
            audio_entry = entry
    if game_entry is None:
        raise directdl.DirectError(f"no hoyo game manifest for {biz}")
    if audio_entry is None:
        raise directdl.DirectError(f"no hoyo audio manifest for language {language}")

    game = _sophon_package(game_entry, f"{game_tag}-{version}")
    audio = _sophon_package(
        audio_entry, f"{game_tag}-{version}-audio-{language}"
    )
    return directdl.Plan(
        game_tag=game_tag,
        version=version,
        archives=(),
        exe=exe,
        sophon=(game, audio),
    )


def _sophon_package(entry: Mapping[str, Any], name: str) -> Any:
    from ..sophon import SophonPackage

    manifest = entry.get("manifest")
    manifest_download = entry.get("manifest_download")
    chunk_download = entry.get("chunk_download")
    stats = entry.get("stats")
    if not all(
        isinstance(value, Mapping)
        for value in (manifest, manifest_download, chunk_download, stats)
    ):
        raise directdl.DirectError(f"hoyo manifest {name} is incomplete")
    if manifest_download.get("encryption") != 0 or chunk_download.get("encryption") != 0:
        raise directdl.DirectError(f"hoyo manifest {name} is encrypted")
    manifest_id = _text(manifest.get("id"), "manifest id")
    prefix = _url(manifest_download.get("url_prefix")).rstrip("/")
    suffix = manifest_download.get("url_suffix") or ""
    if not isinstance(suffix, str):
        raise directdl.DirectError(f"invalid hoyo manifest suffix for {name}")
    return SophonPackage(
        name=directdl.safe_relative_path(name),
        manifest_url=f"{prefix}/{manifest_id}{suffix}",
        chunk_url=_url(chunk_download.get("url_prefix")).rstrip("/"),
        manifest_id=manifest_id,
        size=_size(stats.get("uncompressed_size")),
    )


def _load_from(url: str, fetch: Fetch, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(ungzip(fetch(url, None)))
    except (ValueError, TypeError, OSError) as exc:
        raise directdl.DirectError(f"could not read {label}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise directdl.DirectError(f"{label} response is not an object")
    if payload.get("retcode") != 0:
        raise directdl.DirectError(
            f"{label} API error: {payload.get('message', 'unknown')}"
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise directdl.DirectError(f"{label} response has no data")
    return data


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
