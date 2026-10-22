# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A Nadare-managed ``umu-run`` for hosts without one.

umu-launcher ships its own Steam Linux Runtime container, so it can run a Proton
build on a host that lacks the 32-bit libraries that Proton needs when it is
started directly. This module downloads the published zipapp once (pinned by
sha256), verifies it, and exposes the resulting executable. Nothing here runs at
import time or from a status poll: :func:`ensure_managed` is only reached from a
launch/install path via :func:`resolve`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import download, hosting, paths, process
from .events import Cancel, Emitter, Event, emitter
from .runners import _github_asset_host_allowed

#: Pinned release, verified against the published asset.
UMU_VERSION = "1.4.4"
UMU_TAR_SHA256 = "eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851"

#: The archive member we install; everything else in the tar is ignored.
_MEMBER = "umu/umu-run"

log = logging.getLogger(__name__)


class UmuError(RuntimeError):
    """The managed umu copy could not be set up."""


def managed_path() -> Path:
    """Where Nadare keeps its own umu for this pinned version."""
    return paths.data_dir() / "umu" / UMU_VERSION / "umu-run"


def managed() -> Path | None:
    """The installed managed umu, or ``None``. Pure lookup, never downloads.

    The zipapp has a ``#!/usr/bin/python3`` shebang, so a copy is only usable
    when a system ``python3`` exists. A venv/version-manager shim is not enough:
    the game environment strips those from ``PATH`` (see
    :func:`process.sanitize_game_env`).
    """
    path = managed_path()
    if not (path.is_file() and os.access(path, os.X_OK)):
        return None
    if shutil.which("python3") is None:
        return None
    return path


def _extract_member(archive: Path, destination: Path) -> None:
    """Copy only the regular ``umu/umu-run`` member to ``destination``."""
    tmp: Path | None = None
    try:
        with tarfile.open(archive, "r:*") as tar:
            try:
                member = tar.getmember(_MEMBER)
            except KeyError:
                raise UmuError(f"the umu archive has no {_MEMBER}") from None
            if (
                not member.isfile()
                or member.issym()
                or member.islnk()
                or member.name.startswith(("/", "\\"))
                or ".." in Path(member.name).parts
            ):
                raise UmuError("the umu archive entry is not a safe regular file")
            source = tar.extractfile(member)
            if source is None:
                raise UmuError("the umu archive entry cannot be read")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".umu-run-", dir=destination.parent)
            tmp = Path(name)
            with os.fdopen(fd, "wb") as out, source:
                shutil.copyfileobj(source, out)
        tmp.chmod(0o755)
        os.replace(tmp, destination)
        tmp = None
    except UmuError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise UmuError(f"could not extract umu: {exc}") from exc
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _smoke_test(python: str, executable: Path) -> None:
    try:
        result = subprocess.run(
            [python, str(executable), "--version"],
            env=process.sanitize_game_env(os.environ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UmuError(f"the downloaded umu does not run: {exc}") from exc
    if result.returncode != 0:
        raise UmuError("the downloaded umu does not run")


def ensure_managed(
    *, emit: Emitter | None = None, cancel: Cancel | None = None
) -> Path:
    """Return an installed managed umu, downloading and verifying it once.

    Raises :class:`UmuError` when ``python3`` is unavailable, the download
    fails its pinned hash, the archive is malformed, or the resulting zipapp
    cannot run. A failed install leaves nothing at :func:`managed_path`.
    """
    existing = managed()
    if existing is not None:
        return existing

    python = shutil.which("python3")
    if python is None:
        raise UmuError("python3 is needed to run umu")

    say = emitter(emit)
    destination = managed_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    cache = paths.cache_dir() / "umu"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"umu-launcher-{UMU_VERSION}-zipapp.tar"

    say(Event("step", "Setting up umu (one-time, <1 MB)"))
    try:
        result = download.download(
            hosting.umu_zipapp_url(),
            archive,
            expected_sha256=UMU_TAR_SHA256,
            headers={"Accept": "application/octet-stream"},
            emit=emit,
            cancel=cancel,
            redirect_allowed=_github_asset_host_allowed,
        )
    except (download.DownloadError, OSError) as exc:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise UmuError(f"could not download umu: {exc}") from exc

    try:
        _extract_member(result.path, destination)
        try:
            _smoke_test(python, destination)
        except UmuError:
            destination.unlink(missing_ok=True)
            raise
    finally:
        try:
            result.path.unlink(missing_ok=True)
        except OSError:
            pass

    say(Event("done", "umu is ready", data={"path": str(destination)}))
    return destination


def resolve(
    configured: str = "umu-run",
    *,
    allow_download: bool,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
) -> str | None:
    """Resolve a usable umu argv[0].

    A system copy (or the profile's configured binary) always wins. Otherwise an
    already-installed managed copy is used. Only when ``allow_download`` is true
    do we try to install the managed copy; any failure is logged and reported as
    ``None`` so the caller can fall back to its previous behaviour.
    """
    found = shutil.which(configured)
    if found:
        return found

    existing = managed()
    if existing is not None:
        return str(existing)

    if not allow_download:
        return None

    try:
        return str(ensure_managed(emit=emit, cancel=cancel))
    except UmuError as exc:
        log.info("managed umu unavailable: %s", exc)
        return None


__all__ = [
    "UMU_TAR_SHA256",
    "UMU_VERSION",
    "UmuError",
    "ensure_managed",
    "managed",
    "managed_path",
    "resolve",
]
