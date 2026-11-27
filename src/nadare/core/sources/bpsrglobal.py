# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Blue Protocol: Star Resonance (global, A Plus).

The publisher serves three plain text files under ``https://pc-download.playbpsr.com/apj_obt/``:

* ``branch.txt`` names the active release tree (``<tree>=release``);
* ``<tree>/last/verlist.txt`` gives the current version and the MD5 of ``md5list.txt``;
* ``<tree>/last/md5list.txt`` lists every installed file as ``md5,size,path`` (some rows append
  ``patch_md5,patch_size`` for optional diffs, which Nadare ignores).

Fresh installs download ``<tree>/<version>/game_mini.zip`` (unwrapped without its leading
``game_mini/`` folder); updates, verifies and repairs use the complete per-file manifest at
``<tree>/<version>/game_mini/<path>``. Every request and redirect stays on
``pc-download.playbpsr.com`` over HTTPS.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

from .. import directdl
from . import Fetch, default_fetch

HOST = "pc-download.playbpsr.com"
BASE_URL = f"https://{HOST}/apj_obt"
BRANCH_URL = f"{BASE_URL}/branch.txt"
EXE = "BPSR.exe"
ZIP_PREFIX = "game_mini/"

_TREE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


def url_allowed(url: str) -> bool:
    """True only for HTTPS URLs on ``pc-download.playbpsr.com`` (default port, no userinfo)."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return (parts.hostname or "").lower() == HOST


def _default_fetch(url: str, headers: Mapping[str, str] | None = None) -> bytes:
    """Fetch a feed document with retries, keeping redirects on ``pc-download.playbpsr.com``."""
    return default_fetch(url, headers, redirect_allowed=url_allowed)


def _decode(raw: bytes, what: str) -> str:
    if not isinstance(raw, bytes):
        raise directdl.DirectError(f"bpsr: {what} is not bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise directdl.DirectError(f"bpsr: {what} is not valid UTF-8") from exc


def parse_branch(raw: bytes) -> str:
    """Return the active ``=release`` tree name from ``branch.txt``."""
    text = _decode(raw, "branch.txt")
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        tree, sep, channel = stripped.partition("=")
        if not sep:
            raise directdl.DirectError(f"bpsr: malformed branch.txt line {stripped!r}")
        if channel.strip() != "release":
            continue
        name = tree.strip()
        if not _TREE.fullmatch(name) or "/" in name or "\\" in name or name in (".", ".."):
            raise directdl.DirectError(f"bpsr: unsafe tree name {name!r}")
        directdl.safe_relative_path(name)
        found.append(name)
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        raise directdl.DirectError("bpsr: branch.txt does not name a single release tree")
    return unique[0]


def parse_verlist(raw: bytes) -> tuple[str, str]:
    """Return ``(version, md5list_md5)`` from ``verlist.txt``."""
    text = _decode(raw, "verlist.txt")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise directdl.DirectError("bpsr: verlist.txt is empty")
    parts = lines[0].split(",")
    if len(parts) < 2:
        raise directdl.DirectError("bpsr: verlist.txt has too few columns")
    version = parts[0].strip()
    md5_hex = parts[1].strip()
    if not _VERSION.fullmatch(version):
        raise directdl.DirectError(f"bpsr: bad version {version!r}")
    if not _MD5.fullmatch(md5_hex):
        raise directdl.DirectError(f"bpsr: bad md5list hash {md5_hex!r}")
    return version, md5_hex.lower()


def parse_md5list(raw: bytes, *, file_base: str) -> tuple[tuple[directdl.RemoteFile, ...], int]:
    """Parse ``md5list.txt`` into per-file :class:`~nadare.core.directdl.RemoteFile` items."""
    text = _decode(raw, "md5list.txt")
    base = file_base.rstrip("/")
    files: list[directdl.RemoteFile] = []
    seen: set[str] = set()
    total_bytes = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) not in (3, 5):
            raise directdl.DirectError(f"bpsr: malformed md5list row: {line!r}")
        md5_raw, size_raw, path_raw = parts[0], parts[1], parts[2]
        if not _MD5.fullmatch(md5_raw):
            raise directdl.DirectError(f"bpsr: bad MD5 {md5_raw!r}")
        if not (size_raw.isascii() and size_raw.isdigit()):
            raise directdl.DirectError(f"bpsr: bad size {size_raw!r}")
        size = int(size_raw)
        path = directdl.safe_relative_path(path_raw)
        if path.lower().endswith(".hdiff"):
            continue
        key = path.lower()
        if key in seen:
            raise directdl.DirectError(f"bpsr: {path!r} is listed twice")
        seen.add(key)
        url = f"{base}/{quote(path, safe='/')}"
        if not url_allowed(url):
            raise directdl.DirectError(f"bpsr: disallowed file URL {url!r}")
        files.append(directdl.RemoteFile(path=path, url=url, size=size, md5=md5_raw.lower()))
        total_bytes += size
    if not files:
        raise directdl.DirectError("bpsr: md5list.txt lists no files")
    if EXE.lower() not in seen:
        raise directdl.DirectError(f"bpsr: md5list.txt has no {EXE}")
    return tuple(files), total_bytes


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    """Resolve the current global Blue Protocol: Star Resonance per-file manifest plan."""
    get = fetch or _default_fetch
    tree = parse_branch(get(BRANCH_URL, None))
    ver_raw = get(f"{BASE_URL}/{tree}/last/verlist.txt", None)
    version, expected_md5 = parse_verlist(ver_raw)
    md5list_raw = get(f"{BASE_URL}/{tree}/last/md5list.txt", None)
    if hashlib.md5(md5list_raw).hexdigest() != expected_md5:
        raise directdl.DirectError("bpsr: md5list.txt hash does not match verlist.txt")
    file_base = f"{BASE_URL}/{tree}/{version}/game_mini"
    files, total_bytes = parse_md5list(md5list_raw, file_base=file_base)
    archive_url = f"{BASE_URL}/{tree}/{version}/game_mini.zip"
    return directdl.Plan(
        game_tag="bpsr",
        version=version,
        files=files,
        exe=EXE,
        redirect_allowed=url_allowed,
        extra={
            "tree": tree,
            "archive_url": archive_url,
            "total_bytes": total_bytes,
        },
    )


def fresh_archive_plan(plan: directdl.Plan) -> directdl.Plan:
    """Build the single-zip fresh-install plan for a resolved BPSR manifest ``plan``."""
    archive_url = str(plan.extra.get("archive_url") or "")
    if not url_allowed(archive_url):
        raise directdl.DirectError(f"bpsr: disallowed archive URL {archive_url!r}")
    archive = directdl.Archive(
        name="game_mini.zip",
        source=directdl.RemoteFile(path="game_mini.zip", url=archive_url),
        unwrap=("zip",),
        stream=False,  # streaming cannot apply the redirect allow-list yet (directdl)
        strip_prefix=ZIP_PREFIX,
    )
    return directdl.Plan(
        game_tag=plan.game_tag,
        version=plan.version,
        archives=(archive,),
        exe=plan.exe,
        redirect_allowed=plan.redirect_allowed,
        extra=dict(plan.extra),
    )
