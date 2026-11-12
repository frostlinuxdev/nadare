# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Update the running AppImage in place (#771).

The update banner used to send people to the release page. When Nadare runs from a writable
AppImage it can instead download the new file itself: check its SHA-256 against the hash the
Nadare index publishes (and the release's ``SHA256SUMS``, which must agree), put it next to the
old one, keep the old one until the new one has started, and restart on request.

Nothing here talks to the GUI. The user is always asked first; this module only knows how.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import download, indexfetch, sandbox
from .updatecheck import UpdateInfo

log = logging.getLogger(__name__)

#: Hosts a GitHub release download may come from (the asset URL redirects to a CDN host).
_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_RELEASE_PAGE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/releases/tag/(v[0-9][A-Za-z0-9._+-]*)$"
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64})[ \t]+\*?(.+?)\s*$")
#: Environment the AppImage runtime sets; a child started from the new file must not inherit it.
_APPIMAGE_ENV = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")
INDEX_DOC = "app/releases.json"
BACKUP_SUFFIX = ".previous"
_SUMS_MAX_BYTES = 64 * 1024


class SelfUpdateError(Exception):
    """The update was refused or failed; the message is meant for the user."""


@dataclass(frozen=True)
class AssetUrls:
    appimage: str
    sums: str
    filename: str

    @property
    def signature(self) -> str:
        """The detached release signature published next to the AppImage (``release/sign.sh``)."""
        return self.appimage + ".asc"


@dataclass(frozen=True)
class Expected:
    sha256: str
    #: Where the hash came from; always ``"index"`` (the release's own SHA256SUMS never decides).
    source: str


def asset_urls(info: UpdateInfo) -> AssetUrls | None:
    """Where the new AppImage and its checksums live, or ``None`` when we cannot say.

    Only GitHub release pages are understood; any other host keeps the old banner behaviour.
    """
    match = _RELEASE_PAGE.match(info.url or "")
    if match is None:
        return None
    owner, repo, tag = match.groups()
    if tag[1:] != info.version or ".." in owner + repo:
        return None
    base = f"https://github.com/{owner}/{repo}/releases/download/{tag}/"
    filename = f"Nadare-{info.version}-x86_64.AppImage"
    return AssetUrls(base + filename, base + "SHA256SUMS", filename)


def download_host_allowed(url: str) -> bool:
    """True for an https URL on a GitHub release host (also checked on every redirect)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _DOWNLOAD_HOSTS


#: Why Nadare can only offer Download (#1247); "" means it can replace itself.
MANAGED = "managed"
NOT_APPIMAGE = "not-appimage"
NOT_WRITABLE = "not-writable"
SHARED_FOLDER = "shared-folder"
NO_ASSET = "no-asset"


def why_not_replaceable(env: Mapping[str, str] | None = None) -> str:
    """Why the running AppImage cannot be replaced by Nadare ("" when it can)."""
    env = os.environ if env is None else env
    if env.get("NADARE_MANAGED_UPDATES") == "1" or sandbox.in_flatpak():
        return MANAGED
    if platform.machine() != "x86_64":
        return NOT_APPIMAGE
    raw = env.get("APPIMAGE")
    if not raw:
        return NOT_APPIMAGE
    path = Path(raw)
    if not path.name.endswith(".AppImage") or not path.is_file():
        return NOT_APPIMAGE
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return NOT_WRITABLE
    if not _only_this_user_controls(path):
        return SHARED_FOLDER
    return ""


def appimage_target(env: Mapping[str, str] | None = None) -> Path | None:
    """The AppImage file this process runs from, if Nadare may replace it; else ``None``."""
    env = os.environ if env is None else env
    if why_not_replaceable(env):
        return None
    return Path(env["APPIMAGE"])


_MOVE_OR_TOOL = (
    "Move the AppImage to a folder you own (for example ~/Applications) and Nadare can update "
    "itself there, or let Gear Lever or AppImageUpdate keep it up to date "
    "(the AppImage carries their update information)."
)


def download_only_reason(code: str, path: Path | None) -> tuple[str, str]:
    """``(short, detail)`` telling the user why Update is Download, and what to do (#1247)."""
    if not code:
        return "", ""
    folder = str(path.parent) if path is not None else "its folder"
    if code == NOT_WRITABLE:
        return (
            "Nadare cannot replace itself in this folder.",
            f"You cannot write to {folder}, so Nadare cannot put the new version there. "
            + _MOVE_OR_TOOL,
        )
    if code == SHARED_FOLDER:
        return (
            "Nadare will not replace itself in a folder others can change.",
            f"{folder} (or the AppImage) can be changed by another user, so a self-update there "
            "would not be safe. " + _MOVE_OR_TOOL,
        )
    if code == MANAGED:
        return (
            "Your package manager updates this copy of Nadare.",
            "Update Nadare the way you installed it (Flatpak, AUR or your distribution's "
            "package manager).",
        )
    if code == NO_ASSET:
        return (
            "This release has no AppImage Nadare can install by itself.",
            "Download the new version from the release page.",
        )
    return (
        "This copy of Nadare is not running as an AppImage, so it cannot update itself.",
        "Download the new version from the release page.",
    )


def _only_this_user_controls(path: Path) -> bool:
    """The AppImage is ours, and no other user can swap files in its folder (#811).

    A world-writable folder needs the sticky bit; a group-writable one is fine only for the
    user's own group (umask 002 makes 775 folders common on per-user-group systems).
    """
    try:
        info = path.stat()
        folder = path.parent.stat()
    except OSError:
        return False
    if info.st_uid != os.getuid():
        return False
    mode = folder.st_mode
    if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
        return False
    if mode & stat.S_IWGRP and folder.st_gid != os.getgid():
        return False
    return True


def _index_document() -> Any:
    return indexfetch.fetch_json(INDEX_DOC, validate=_looks_like_release_doc).data


def _looks_like_release_doc(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("releases"), dict)


def _fetch_sums(url: str) -> str:
    with download.http_open(url, timeout=15, redirect_allowed=download_host_allowed) as resp:
        data = resp.read(_SUMS_MAX_BYTES + 1)
    if len(data) > _SUMS_MAX_BYTES:
        raise download.DownloadError(f"checksum file too large (over {_SUMS_MAX_BYTES} bytes)")
    return data.decode("utf-8", "replace")


def _index_hash(info: UpdateInfo, fetch_index: Callable[[], Any]) -> str | None:
    try:
        doc = fetch_index()
    except Exception as exc:  # noqa: BLE001 - the release sums can still answer
        log.debug("self-update: the index is unavailable: %s", exc)
        return None
    if not isinstance(doc, dict):
        return None
    releases = doc.get("releases")
    entry = releases.get(info.version) if isinstance(releases, dict) else None
    entry = entry.get("x86_64") if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return None
    value = entry.get("sha256")
    if not isinstance(value, str) or not _SHA256.match(value):
        raise SelfUpdateError("the Nadare index lists a malformed checksum for this release")
    return value.lower()


def _sums_hash(filename: str, text: str) -> str | None:
    for line in text.splitlines():
        match = _SUMS_LINE.match(line)
        if match and match.group(2) == filename:
            return match.group(1).lower()
    return None


def expected_sha256(
    info: UpdateInfo,
    *,
    fetch_sums: Callable[[str], str] | None = None,
    fetch_index: Callable[[], Any] | None = None,
) -> Expected:
    """The hash the new AppImage must have, or :class:`SelfUpdateError` when none is known.

    Only the index can supply it: the release's own ``SHA256SUMS`` sits next to the AppImage, so
    it cannot vouch for it. The sums are read only to cross-check the index; if the two disagree
    the update is refused.
    """
    urls = asset_urls(info)
    if urls is None:
        raise SelfUpdateError("this release cannot be updated in place")
    indexed = _index_hash(info, fetch_index or _index_document)
    if not indexed:
        raise SelfUpdateError(
            "no checksum is published in the Nadare index for this release yet (or the index "
            "could not be reached); the update was not downloaded. Try again later, or download "
            "it from the release page"
        )
    try:
        listed = _sums_hash(urls.filename, (fetch_sums or _fetch_sums)(urls.sums))
    except Exception as exc:  # noqa: BLE001 - the cross-check is best effort
        log.debug("self-update: could not read %s: %s", urls.sums, exc)
        listed = None
    if listed and indexed != listed:
        raise SelfUpdateError(
            "the release checksums disagree with the Nadare index; the update was not downloaded"
        )
    return Expected(indexed, "index")


def download_verified(
    url: str,
    dest: Path,
    sha256: str,
    *,
    allowed: Callable[[str], bool] = download_host_allowed,
    emit: Any = None,
    cancel: Any = None,
) -> Path:
    """Download ``url`` to ``dest`` and keep it only if its SHA-256 matches; mark it executable."""
    if not allowed(url):
        raise SelfUpdateError("the update is not hosted where Nadare expects it")
    try:
        download.download(
            url,
            dest,
            expected_sha256=sha256.lower(),
            redirect_allowed=allowed,
            emit=emit,
            cancel=cancel,
            resume=False,
        )
    except download.IntegrityError as exc:
        raise download.IntegrityError(
            "the downloaded file does not match its checksum, so it was not installed. "
            "It may be damaged, or it may have been changed on the way."
        ) from exc
    os.chmod(dest, 0o755)
    return dest


def verify_signature(urls: AssetUrls, path: Path) -> None:
    """Refuse ``path`` unless the release's ``.asc`` is the release key's signature over it.

    The hash came from the index, which is not signed: only this check ties the file to the key
    that signs Nadare's releases (#1209 P0-1). A release without a signature is not installed
    in place; the user is sent to the release page instead.
    """
    from . import releasesig

    try:
        text = _fetch_sums(urls.signature)
    except (SelfUpdateError, download.DownloadError, OSError) as exc:
        raise SelfUpdateError(
            "this release has no signature Nadare can check, so it was not installed. "
            "Download it from the release page instead."
        ) from exc
    try:
        releasesig.verify_detached(path, text)
    except (releasesig.SignatureError, OSError) as exc:
        raise SelfUpdateError(
            f"the download is not signed with Nadare's release key ({exc}), so it was not "
            "installed. It may have been changed on the way."
        ) from exc


def install_update(new: Path, target: Path, filename: str, *, sha256: str | None = None) -> Path:
    """Put ``new`` next to ``target`` under ``filename``; keep the old file as ``*.previous``.

    The running AppImage stays valid after being renamed (Linux keeps the open file), so
    nothing is copied. If the new file cannot be moved into place the old one is put back.

    A desktop or Steam shortcut made earlier holds the old file's exact path, so that path (and
    any older link left by a former update) becomes a relative link to the new file: the
    shortcut keeps starting Nadare (#869). A folder that refuses links just loses that nicety.
    """
    dest = target.with_name(filename)
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    if sha256 is not None and _sha256_file(new) != sha256.lower():
        # Checked again just before the swap: the file may have changed since the download
        # was verified (#811).
        raise SelfUpdateError("the downloaded file changed after it was checked; not installed")
    os.chmod(new, 0o755)
    try:
        os.replace(target, backup)
    except OSError as exc:
        raise SelfUpdateError(f"could not set the old version aside: {exc}") from exc
    try:
        os.replace(new, dest)
    except OSError as exc:
        try:
            os.replace(backup, target)
        except OSError:  # pragma: no cover - nothing sensible left to do
            log.error("self-update: could not restore %s from %s", target, backup)
        raise SelfUpdateError(f"could not put the new version in place: {exc}") from exc
    if dest != target:
        _leave_links(target, dest)
    return dest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise SelfUpdateError(f"could not read the download: {exc}") from exc
    return digest.hexdigest()


def _leave_links(old: Path, dest: Path) -> None:
    """Make ``old``, and every link in its folder that led to it, point at ``dest``."""
    stale = [old]
    try:
        for entry in old.parent.glob("*.AppImage"):
            if entry != old and entry.is_symlink() and entry.name != dest.name:
                if os.path.normpath(os.readlink(entry)) in (old.name, str(old)):
                    stale.append(entry)
    except OSError:
        pass
    for path in stale:
        try:
            # A fixed <AppImage>.link name could belong to the user. Stage in a
            # private directory on the same filesystem so replacement stays atomic.
            with tempfile.TemporaryDirectory(prefix=".nadare-link-", dir=path.parent) as work:
                temp = Path(work) / "launcher"
                os.symlink(dest.name, temp)
                os.replace(temp, path)
        except OSError:
            log.debug("self-update: could not leave a link at %s", path, exc_info=True)


def cleanup_previous(running: Path) -> None:
    """Delete the kept ``*.previous`` copy of an older version once the new one has started."""
    keep = running.name + BACKUP_SUFFIX
    for old in running.parent.glob("Nadare-*.AppImage" + BACKUP_SUFFIX):
        if old.name != keep:
            try:
                old.unlink()
            except OSError:
                log.debug("self-update: could not remove %s", old)


#: What the restart helper runs: wait (at most 30 s) for the old process to exit, then start
#: the new file. $0 is the new AppImage, $1 the old pid, the rest its arguments.
_RESTART_SCRIPT = (
    'pid="$1"; shift; n=0; '
    'while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 150 ]; do sleep 0.2; n=$((n+1)); done; '
    'exec "$0" "$@"'
)


def restart_command(
    new: Path, env: Mapping[str, str], *, pid: int | None = None
) -> tuple[list[str], dict[str, str]]:
    """The command (and a clean environment) that starts ``new`` once this process is gone.

    It waits for ``pid`` (this process) to exit instead of sleeping a fixed time: Nadare allows
    one running instance, and a second start while this one is still shutting down would just
    hand over to it and exit, leaving no window at all.
    """
    clean = {key: value for key, value in env.items() if key not in _APPIMAGE_ENV}
    waiting_for = os.getpid() if pid is None else pid
    argv = ["/bin/sh", "-c", _RESTART_SCRIPT, str(new), str(waiting_for), "gui"]
    return argv, clean


def restart(new: Path) -> None:
    """Start ``new`` detached; the caller quits Nadare right after."""
    argv, env = restart_command(new, os.environ)
    subprocess.Popen(  # noqa: S603 - fixed argv built above
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.info("self-update: started %s", new)
    sys.stdout.flush()


__all__ = [
    "AssetUrls",
    "Expected",
    "SelfUpdateError",
    "MANAGED",
    "NOT_APPIMAGE",
    "NOT_WRITABLE",
    "NO_ASSET",
    "SHARED_FOLDER",
    "appimage_target",
    "asset_urls",
    "cleanup_previous",
    "download_host_allowed",
    "download_only_reason",
    "download_verified",
    "expected_sha256",
    "verify_signature",
    "why_not_replaceable",
    "install_update",
    "restart",
    "restart_command",
]
