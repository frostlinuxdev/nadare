# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Published block pins for the installers Nadare downloads and runs.

The NIKKE miniloader and the Endfield launcher installer come from rolling publisher
URLs with no published hash. An index node pins each one (trust on first use) under its
own ``catalog/archive-pins/<tag>.json``; a client only uses the pin when it names the
exact URL it is downloading, and refuses an installer with no pin (or a malformed one)
before downloading anything (#1593). Arknights needs none: with the index on it installs
from the signed Yostar manifest, and its setup.exe only runs with the index off. A URL
the user typed in the profile is the user's own choice, not an index entry, and is not
subject to this rule.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from pathlib import Path

from . import archivepins, directdl, download
from .backends import BackendError

#: The archive name an installer pin document uses for its one part.
ARCHIVE = "installer"


class InstallerPinError(Exception):
    """A published installer pin does not match, or could not be resolved."""


def pin_for(tag: str, url: str) -> archivepins.ArchivePin | None:
    """The published pin for ``tag`` when it describes exactly ``url``."""
    try:
        pin = archivepins.load(tag).get(ARCHIVE)
        if pin is not None and len(pin.parts) == 1 and pin.parts[0].url == url:
            return pin
    except Exception:  # noqa: BLE001 - the caller refuses a missing or unreadable pin
        return None
    return None


def require(tag: str, url: str, *, entry: str | None = None) -> archivepins.ArchivePin:
    """Resolve and validate the exact installer pin before making any file request."""
    pin = pin_for(tag, url)
    if pin is not None:
        try:
            # Validate even an in-memory pin with the document's strict size/hash rules.
            archivepins.parse_document(archivepins.document({ARCHIVE: pin}))
            return pin
        except (ValueError, TypeError, AttributeError):
            pass
    raise BackendError(
        f"the index entry for {entry or tag} has no checksum; "
        "Nadare will not run an unpinned installer"
    )


def verify_file(path: Path, pin: archivepins.ArchivePin) -> None:
    """Check ``path`` against the pin's one part; raise :class:`InstallerPinError`."""
    try:
        archivepins.verify_file(path, pin.parts[0], pin.block_size)
    except (archivepins.PinError, OSError) as exc:
        raise InstallerPinError(str(exc)) from exc


def check(
    tag: str,
    url: str,
    path: Path,
    *,
    info: Callable[[str], None],
) -> None:
    """Verify a downloaded installer against its required pin."""
    pin = require(tag, url)
    try:
        verify_file(path, pin)
    except InstallerPinError:
        path.unlink(missing_ok=True)
        raise


def installer_plan(tag: str, url: str, size: int | None) -> directdl.Plan:
    """A one-archive plan whose source is the installer at ``url``."""
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or "installer.exe"
    path = directdl.safe_relative_path(name)
    source = directdl.RemoteFile(path=path, url=url, size=size)
    archive = directdl.Archive(name=ARCHIVE, source=source)
    return directdl.Plan(game_tag=tag, version="1", archives=(archive,), exe=path)


def resolve(
    tag: str,
    *,
    fetch_json: Callable[[str], object] | None = None,
    head: Callable[[str], int | None] | None = None,
) -> directdl.Plan:
    """Resolve the current installer URL for ``tag`` and make its plan."""
    get_json = download.fetch_json if fetch_json is None else fetch_json
    get_size = download.remote_size if head is None else head
    try:
        if tag == "nikke-installer":
            from .sources import nikke

            url = nikke.MINILOADER_URL
        elif tag == "endfield-installer":
            from . import endfield_shim

            payload = get_json(endfield_shim.LAUNCHER_API)
            url = payload.get("exe_url") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not endfield_shim.installer_host_allowed(url):
                raise InstallerPinError(
                    "the Endfield launcher API did not return an installer on an allowed host"
                )
        else:
            raise InstallerPinError(f"unknown installer tag {tag!r}")
        size = get_size(url)
    except InstallerPinError:
        raise
    except Exception as exc:  # noqa: BLE001 - every network/API problem is a pin failure
        raise InstallerPinError(f"cannot resolve {tag}: {exc}") from exc
    return installer_plan(tag, url, size)
