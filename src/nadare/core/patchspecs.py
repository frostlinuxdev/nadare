# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Shipped patch specs, keyed by backend kind.

A backend declares the overlays it understands as data (``patches`` in its catalog entry or a
``patches`` class attribute) and :mod:`nadare.core.patches` does the applying. Nothing here is
executed from the download: entries carry hashes and sizes only, and every destination is a
plain relative path inside the install folder.

Adding a spec means tracking one upstream release:

1. download the release archive and record its SHA-256 (``archive_sha256``) plus its size;
2. for each member, record the resulting file's SHA-256 and size (``sha256``/``size``);
3. record the pre-patch file's SHA-256 and size from a pristine install
   (``before_sha256``/``before_size``) -- without both, ``apply`` will accept any build;
4. pin the build: the ``ManifestFiles/0release_<pkg>_<build>.bytes`` names under
   ``AzurPromiliaJP_Data/.res/`` are what a spec's hashes are tied to.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import indexfetch
from .patches import PatchFile, PatchSpec

log = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_KIBOTL_MEMBER_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(bytes|r)$")
_GAME_BUILD_RE = re.compile(r"^[0-9]{1,20}$")
_AZUR_MANIFEST_RE = re.compile(r"^0release_[a-z0-9_]+_([0-9]{1,20})\.bytes$")


@dataclass(frozen=True)
class PatchIndexRule:
    """Validation policy for an index-published update to a built-in patch spec."""

    kind: str
    index_file: str
    url_prefix: str
    dest_root: str
    dest_suffix: str
    member_pattern: re.Pattern[str]
    max_files: int = 16
    max_archive_size: int = 1 << 30

#: KiboTL's English overlay for Azur Promilia (JP), the 2026-11-27 16:13 UTC upload of en.zip
#: under tag ``v1.0-3219598``, for game build 3273853. Replaces three Unity containers.
#:
#: ``vstring.bytes`` and ``table_data.bytes`` are replaced by the same overlay and must stay
#: together: applying ``table_data.bytes`` alone raises ``ArgumentNullException`` in
#: ``Azur.Gameplay.Flows.LoadPlayerTask`` and hangs the loading screen (issue #1866).
#:
#: The 3273853 resource update renamed the three text containers. The upload's ``startup.r``
#: and ``onest-font.r`` are left out: they do not fit the 11-24 client's StreamingAssets (on a
#: real install, images and text land in the wrong places), while the three text files alone
#: play fine.
KIBOTL_EN = PatchSpec(
    id="kibotl-en",
    title="KiboTL English overlay",
    version="v1.0-3273853",
    url="https://github.com/KiboTL/text/releases/download/v1.0-3219598/en.zip",
    game_build="3273853",
    # KiboTL re-uploads en.zip under the same tag; this is the 11-27 16:13 UTC upload.
    archive_sha256="bb145e147e59ade1a327ee4b620d1412b17042b329febda456f50f901a46b043",
    archive_size=209693417,
    files=(
        PatchFile(
            member="vstring.bytes",
            dest="AzurPromiliaJP_Data/.res/raw_base_package/BundleFiles/heqmts9lcjdvbupu0x2wpg.r",
            sha256="1dab6f55d9f8c339cad95845390354f1504d51f033bd523bffa02dbcba4813ea",
            size=140506137,
            before_sha256="60e09fa31ac7e43e2a74cb7e722cf19b3331a188817125a086a57a17a34987ee",
            before_size=134055486,
        ),
        PatchFile(
            member="table_data.bytes",
            dest=(
                "AzurPromiliaJP_Data/.res/raw_streaming_package/BundleFiles/"
                "5g1m5b4sbqlfalcxoaquw.r"
            ),
            sha256="b6255ead4d4e2b8ea9cd9442e78c8b619433747edef08b42ed471081f9fb500f",
            size=28840130,
            before_sha256="4369839fd46cd2c821f33dc2b92c46cc87f4413a215be0abe17461ec743896c2",
            before_size=28840130,
        ),
        PatchFile(
            member="static-text.r",
            dest="AzurPromiliaJP_Data/.res/default_package/BundleFiles/h3y7ujukwn9qbhoqhzkkg.r",
            sha256="02aaed5b1a61370e356aced8b52b87d8872bf491204cae2ce5b35f07cb69afc0",
            size=4991773,
            before_sha256="9771462bacb96c7ee544fe53d0d2a4e56e3576b1336d2a2ec6653bfef1a6a138",
            before_size=4993822,
        ),
    ),
    notes=(
        "Pure container swap: the client is not patched, so a game update overwrites these "
        "files. Check status() after every update; a build change shows up as 'unknown' and "
        "apply refuses instead of writing mismatched containers."
    ),
)

KIBOTL_EN_RULE = PatchIndexRule(
    kind="azurpromilia-jp-yostar",
    index_file="patches/kibotl-en.json",
    url_prefix="https://github.com/KiboTL/text/releases/download/",
    dest_root="AzurPromiliaJP_Data/",
    dest_suffix=".r",
    member_pattern=_KIBOTL_MEMBER_RE,
    max_files=16,
    max_archive_size=1 << 30,
)

#: Built-in patch id -> its index validation rule.
INDEX_RULES: dict[str, PatchIndexRule] = {
    KIBOTL_EN.id: KIBOTL_EN_RULE,
}

#: Backend kind -> the overlays it offers.
SPECS: dict[str, tuple[PatchSpec, ...]] = {
    "azurpromilia-jp-yostar": (KIBOTL_EN,),
}


def _valid_url(url: object, prefix: str) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    if "\\" in url or "?" in url or "#" in url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(prefix)
        parsed_port = parsed.port
        base_port = base.port
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return None
    if parsed.netloc.lower() != base.netloc.lower():
        return None
    if (parsed.hostname or "").lower() != (base.hostname or "").lower():
        return None
    if parsed_port != base_port:
        return None
    if parsed.query or parsed.fragment:
        return None
    if not parsed.path.startswith(base.path) or len(parsed.path) <= len(base.path):
        return None
    for candidate in (parsed.path, urllib.parse.unquote(parsed.path)):
        if "\\" in candidate or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
            return None
        if not candidate.startswith(base.path):
            return None
        segments = candidate.split("/")[1:]
        if not segments or any(seg in ("", ".", "..") for seg in segments):
            return None
    return url


def _valid_version(version: object) -> str | None:
    if not isinstance(version, str) or ".." in version or not _VERSION_RE.fullmatch(version):
        return None
    return version


def _reject(patch_id: str, reason: str) -> None:
    log.warning("rejecting patch index for %s: %s", patch_id, reason)
    return None


def spec_from_index(
    builtin: PatchSpec, data: Any, *, rule: PatchIndexRule | None = None
) -> PatchSpec | None:
    """Validate an index document against ``builtin`` and its rule, or return ``None``.

    Always constructs and returns a fresh :class:`PatchSpec` object (never ``builtin`` itself),
    so :func:`is_builtin_fallback` can distinguish an index-sourced spec from a built-in
    fallback by object identity even when the hashes match.
    """
    active_rule = rule or INDEX_RULES.get(builtin.id)
    if active_rule is None:
        return _reject(builtin.id, "no index rule defined")
    if not isinstance(data, dict):
        return _reject(builtin.id, "document is not a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != 1:
        return _reject(builtin.id, f"unsupported schema {schema!r}")
    if data.get("id") != builtin.id:
        return _reject(builtin.id, f"mismatched id {data.get('id')!r}")
    if data.get("kind") != active_rule.kind:
        return _reject(builtin.id, f"mismatched kind {data.get('kind')!r}")
    version = _valid_version(data.get("version"))
    if version is None:
        return _reject(builtin.id, f"invalid version {data.get('version')!r}")
    url = _valid_url(data.get("url"), active_rule.url_prefix)
    if url is None:
        return _reject(builtin.id, f"disallowed url {data.get('url')!r}")
    archive_sha256 = data.get("archive_sha256")
    if not isinstance(archive_sha256, str) or not _SHA256_RE.fullmatch(archive_sha256):
        return _reject(builtin.id, "invalid archive_sha256")
    archive_size = data.get("archive_size")
    if type(archive_size) is not int or not (0 < archive_size <= active_rule.max_archive_size):
        return _reject(builtin.id, f"invalid archive_size {archive_size!r}")
    game_build = data.get("game_build")
    if game_build is not None and not (
        isinstance(game_build, str) and _GAME_BUILD_RE.fullmatch(game_build)
    ):
        return _reject(builtin.id, f"invalid game_build {game_build!r}")
    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not (1 <= len(raw_files) <= active_rule.max_files):
        return _reject(builtin.id, "invalid files list")

    parsed_files: list[PatchFile] = []
    for item in raw_files:
        if not isinstance(item, dict):
            return _reject(builtin.id, "file entry is not an object")
        member = item.get("member")
        if (
            not isinstance(member, str)
            or "/" in member
            or "\\" in member
            or ".." in member
            or not active_rule.member_pattern.fullmatch(member)
        ):
            return _reject(builtin.id, f"disallowed member {member!r}")
        dest = item.get("dest")
        if (
            not isinstance(dest, str)
            or not dest.startswith(active_rule.dest_root)
            or not dest.endswith(active_rule.dest_suffix)
            or len(dest) <= len(active_rule.dest_root) + len(active_rule.dest_suffix)
            or dest.endswith("/" + active_rule.dest_suffix)
        ):
            return _reject(builtin.id, f"disallowed dest {dest!r}")
        size = item.get("size")
        if type(size) is not int or not (0 <= size <= active_rule.max_archive_size):
            return _reject(builtin.id, f"invalid size for {member!r}")
        before_sha256 = item.get("before_sha256")
        before_size = item.get("before_size")
        if before_sha256 is None or before_size is None:
            return _reject(builtin.id, f"missing before pin for {member!r}")
        if type(before_size) is not int or not (0 <= before_size <= active_rule.max_archive_size):
            return _reject(builtin.id, f"invalid before_size for {member!r}")
        try:
            parsed_files.append(
                PatchFile(
                    member=member,
                    dest=dest,
                    sha256=item.get("sha256"),
                    size=size,
                    before_sha256=before_sha256,
                    before_size=before_size,
                )
            )
        except (TypeError, ValueError) as exc:
            return _reject(builtin.id, str(exc))

    try:
        return PatchSpec(
            id=builtin.id,
            title=builtin.title,
            version=version,
            url=url,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
            files=tuple(parsed_files),
            notes=builtin.notes,
            requires=builtin.requires,
            # Not inherited: a newer pin may be for a newer build (#1895).
            game_build=game_build,
        )
    except (TypeError, ValueError) as exc:
        return _reject(builtin.id, str(exc))


# --------------------------------------------------------------------------- game build (#1895)


def _azur_build(root: Path) -> str | None:
    """Azur Promilia's resource build: the highest ``0release_<pkg>_<build>.bytes`` number.

    A hot update writes new manifests under ``AzurPromiliaJP_Data/.res`` and can leave the old
    ones next to them, so the highest number is the live one. Lists folders only, no hashing.
    """
    best: int | None = None
    for base in ("AzurPromiliaJP_Data/.res", "AzurPromiliaJP_Data/StreamingAssets/.res"):
        try:
            packages = list((root / base).iterdir())
        except OSError:
            continue
        for package in packages:
            try:
                names = [entry.name for entry in (package / "ManifestFiles").iterdir()]
            except OSError:
                continue
            for name in names:
                match = _AZUR_MANIFEST_RE.fullmatch(name)
                if match:
                    number = int(match.group(1))
                    best = number if best is None else max(best, number)
    return None if best is None else str(best)


#: Backend kind -> a reader for the installed game build (``None`` when it cannot tell).
BUILD_PROBES: dict[str, Callable[[Path], str | None]] = {
    "azurpromilia-jp-yostar": _azur_build,
}


def installed_build(kind: str, root: Path) -> str | None:
    """The game build installed at ``root``, or ``None`` (no probe, or nothing readable)."""
    probe = BUILD_PROBES.get(kind)
    if probe is None:
        return None
    try:
        return probe(Path(root))
    except OSError:
        return None


def is_builtin_fallback(spec: PatchSpec) -> bool:
    """True when ``spec`` is one of the built-in fallback objects in :data:`SPECS` (by identity).

    Only :func:`_resolve_cached` and :func:`refresh` return those objects, and only when no
    valid index document exists; :func:`spec_from_index` always builds a new :class:`PatchSpec`
    object.
    """
    return any(spec is builtin for group in SPECS.values() for builtin in group)


def _resolve_cached(builtin: PatchSpec) -> PatchSpec:
    """Return a fresh index spec from cache when valid, else the ``builtin`` fallback object."""
    rule = INDEX_RULES.get(builtin.id)
    if rule is None:
        return builtin
    data = indexfetch.read_cached(
        rule.index_file,
        validate=lambda doc, b=builtin, r=rule: spec_from_index(b, doc, rule=r) is not None,
    )
    if data is None:
        return builtin
    return spec_from_index(builtin, data, rule=rule) or builtin


def specs_for(kind: str) -> tuple[PatchSpec, ...]:
    """The overlays a backend kind offers (network-free: cached index or built-in fallback)."""
    return tuple(_resolve_cached(spec) for spec in SPECS.get(kind, ()))


def spec_by_id(kind: str, patch_id: str) -> PatchSpec | None:
    for spec in specs_for(kind):
        if spec.id == patch_id:
            return spec
    return None


def builtin_by_id(patch_id: str) -> tuple[PatchSpec, PatchIndexRule] | None:
    """Return ``(builtin_spec, index_rule)`` for ``patch_id``, or ``None``."""
    rule = INDEX_RULES.get(patch_id)
    if rule is None:
        return None
    for spec in SPECS.get(rule.kind, ()):
        if spec.id == patch_id:
            return spec, rule
    return None


def refresh(kind: str) -> tuple[PatchSpec, ...]:
    """Fetch fresh index pins for ``kind`` (best effort, never raises on index trouble)."""
    out: list[PatchSpec] = []
    for builtin in SPECS.get(kind, ()):
        rule = INDEX_RULES.get(builtin.id)
        if rule is None:
            out.append(builtin)
            continue
        try:
            result = indexfetch.fetch_json(
                rule.index_file,
                validate=lambda doc, b=builtin, r=rule: spec_from_index(b, doc, rule=r) is not None,
            )
            parsed = spec_from_index(builtin, result.data, rule=rule)
            out.append(parsed if parsed is not None else _resolve_cached(builtin))
        except Exception as exc:  # noqa: BLE001 - best-effort index refresh
            log.debug("patch index refresh failed for %s: %s", builtin.id, exc)
            out.append(_resolve_cached(builtin))
    return tuple(out)


__all__ = [
    "INDEX_RULES",
    "KIBOTL_EN",
    "BUILD_PROBES",
    "KIBOTL_EN_RULE",
    "PatchIndexRule",
    "SPECS",
    "builtin_by_id",
    "installed_build",
    "is_builtin_fallback",
    "refresh",
    "spec_by_id",
    "spec_from_index",
    "specs_for",
]
