# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Obtain the Neverness to Everness launcher files from Perfect World.

The game itself is installed by Nadare, but it only starts through the
publisher's launcher, ``NTEGlobal/NTEGlobalLauncher.exe``. The official
installer is an NSIS executable that carries ``WmGpLaunchSetupFiles.7z``,
which in turn contains the ``NTEGlobal`` directory. This module downloads the
installer once, unpacks both layers with 7-Zip and copies just the
``NTEGlobal`` directory into the game's install root.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import download as _dl

#: Official Perfect World installer carrying the launcher payload.
INSTALLER_URL = (
    "https://ntecdn2.perfectworld.com/clientRes/installer-Global/"
    "NTE_Global_common_setup_1.0.6.0423_20260424.exe"
)

#: Pinned from the installer fetched 2026-10-25 (56486040 bytes); update together
#: with :data:`INSTALLER_URL`.
INSTALLER_SHA256 = "345e84bab04f24b0ec4907e297569586ef155087ecc1732f88757e7eb94a4994"

#: Size in bytes of the pinned installer; used by the maintainer canary.
INSTALLER_SIZE = 56486040

#: Name of the archive found inside the NSIS installer.
_INNER_ARCHIVE = "WmGpLaunchSetupFiles.7z"

#: Name of the launcher executable and its directory.
_LAUNCHER_DIR = "NTEGlobal"
_LAUNCHER_EXE = "NTEGlobalLauncher.exe"


class NteLauncherError(RuntimeError):
    """The launcher could not be downloaded, unpacked or installed."""


def launcher_path(install_root: Path) -> Path:
    """Return the launcher executable inside ``install_root``."""
    return install_root / _LAUNCHER_DIR / _LAUNCHER_EXE


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 of ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_installer(cache: Path, download: Callable[[str, Path], Any]) -> Path:
    """Download the installer into ``cache`` once, atomically, and verify its hash."""
    if cache.is_file():
        actual = _sha256(cache)
        if actual == INSTALLER_SHA256:
            return cache
        cache.unlink(missing_ok=True)

    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="NTE_setup-", suffix=".exe.part", dir=cache.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        download(INSTALLER_URL, temporary)
        actual = _sha256(temporary)
        if actual != INSTALLER_SHA256:
            raise NteLauncherError(
                f"{cache.name} SHA-256 mismatch: expected {INSTALLER_SHA256}, got {actual}"
            )
        os.replace(temporary, cache)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return cache


def _extract(sevenz: str, archive: Path, outdir: Path, run: Callable[..., Any]) -> Path:
    """Unpack ``archive`` into ``outdir``; the archive is always the last argument."""
    outdir.mkdir(parents=True, exist_ok=True)
    result = run(
        [sevenz, "x", "-y", f"-o{outdir}", str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NteLauncherError(
            f"cannot extract {archive.name} with {sevenz}: {result.stderr.strip()}"
        )
    return outdir


def _stage_tree(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` via staging, restoring the old tree on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name("." + dest.name + ".tmp")
    backup = dest.with_name("." + dest.name + ".old")
    shutil.rmtree(temporary, ignore_errors=True)
    if backup.exists():
        if dest.exists():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            os.replace(backup, dest)
    try:
        shutil.copytree(src, temporary)
        if dest.exists():
            os.replace(dest, backup)
        try:
            os.replace(temporary, dest)
        except BaseException:
            if backup.exists() and not dest.exists():
                os.replace(backup, dest)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def ensure_launcher(
    install_root: Path,
    *,
    data_dir: Path,
    download: Callable[[str, Path], Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Make sure the publisher launcher exists under ``install_root``."""
    installed = launcher_path(install_root)
    if installed.is_file():
        return installed

    sevenz = which("7z") or which("7zz") or which("7za")
    if not sevenz:
        raise NteLauncherError(
            "7-Zip not found; install p7zip or 7zip so the Neverness to "
            "Everness launcher can be extracted from the official installer"
        )

    if download is None:

        def _download(url: str, dest: Path) -> Any:
            return _dl.download(url, dest)

        download = _download

    cache = _fetch_installer(data_dir / "nte" / "NTE_setup.exe", download)

    with tempfile.TemporaryDirectory(prefix="nte-launcher-") as tmpdir:
        outer = _extract(sevenz, cache, Path(tmpdir) / "installer", run)
        inner = next((path for path in outer.rglob(_INNER_ARCHIVE) if path.is_file()), None)
        if inner is None:
            raise NteLauncherError(f"the installer does not contain {_INNER_ARCHIVE}")

        payload = _extract(sevenz, inner, Path(tmpdir) / "payload", run)
        launcher = next(
            (
                path
                for path in payload.rglob(_LAUNCHER_EXE)
                if path.is_file() and path.parent.name == _LAUNCHER_DIR
            ),
            None,
        )
        if launcher is None:
            raise NteLauncherError(f"the installer payload does not contain {_LAUNCHER_EXE}")

        _stage_tree(launcher.parent, install_root / _LAUNCHER_DIR)

    return launcher_path(install_root)
