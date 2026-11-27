# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Obtain the Perfect World launcher files used by Neverness to Everness and Tower of Fantasy.

Both games only start through the publisher's launcher: NTE's
``NTEGlobal/NTEGlobalLauncher.exe`` and Tower of Fantasy's
``Launcher/HottaLauncher.exe``. Their official installers are NSIS executables that carry
``WmGpLaunchSetupFiles.7z``, which in turn holds the launcher directory. This module downloads
an installer once, unpacks both layers with 7-Zip and copies just the launcher directory into
the game's install root.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import download as _dl
from . import process, sevenzip

#: Official Perfect World installer carrying NTE's launcher payload.
INSTALLER_URL = (
    "https://ntecdn2.perfectworld.com/clientRes/installer-Global/"
    "NTE_Global_common_setup_1.0.6.0423_20260424.exe"
)

#: Pinned from the installer fetched 2026-10-25 (56486040 bytes); update together
#: with :data:`INSTALLER_URL`.
INSTALLER_SHA256 = "345e84bab04f24b0ec4907e297569586ef155087ecc1732f88757e7eb94a4994"

#: Size in bytes of the pinned installer; used by the maintainer canary.
INSTALLER_SIZE = 56486040

#: Official Perfect World installer carrying Tower of Fantasy's launcher payload.
TOF_INSTALLER_URL = (
    "https://htkrydpatch1.wmupd.com/clientRes/htIntlWindowsInstaller/"
    "HT_Intl_setup_1.1.2.0222_20250221.exe"
)

#: Pinned from the installer fetched 2026-11-24 (17725088 bytes); update together
#: with :data:`TOF_INSTALLER_URL`.
TOF_INSTALLER_SHA256 = "ce9e34805e8cafac34fd6d5300dc6c6c251b36ae0fe6b9d2177e665d6c25f1fc"

#: Size in bytes of the pinned Tower of Fantasy installer; used by the maintainer canary.
TOF_INSTALLER_SIZE = 17725088

#: Name of the archive found inside an NSIS installer.
_INNER_ARCHIVE = "WmGpLaunchSetupFiles.7z"

#: NTE's launcher executable and its directory in the payload and the game root.
_LAUNCHER_DIR = "NTEGlobal"
_LAUNCHER_EXE = "NTEGlobalLauncher.exe"


class NteLauncherError(RuntimeError):
    """The NTE launcher could not be downloaded, unpacked or installed."""


class TofLauncherError(RuntimeError):
    """The Tower of Fantasy launcher could not be downloaded, unpacked or installed."""


@dataclass(frozen=True)
class LauncherSpec:
    """The little that differs between the two Perfect World launchers."""

    game: str
    """The game's name, for the 7-Zip message."""
    short: str
    """The short name used in the pinned-installer message."""
    url: str
    sha256: str
    size: int
    inner_archive: str
    payload_dir: str
    """The launcher's directory inside the extracted installer payload."""
    destination: str
    """The directory name the launcher gets under the game's install root."""
    exe: str
    cache_subdir: str
    cache_name: str
    error: type[RuntimeError]


def _nte_spec() -> LauncherSpec:
    """NTE's spec, built per call so a re-pin (or a test) can change the module constants."""
    return LauncherSpec(
        game="Neverness to Everness",
        short="NTE",
        url=INSTALLER_URL,
        sha256=INSTALLER_SHA256,
        size=INSTALLER_SIZE,
        inner_archive=_INNER_ARCHIVE,
        payload_dir=_LAUNCHER_DIR,
        destination=_LAUNCHER_DIR,
        exe=_LAUNCHER_EXE,
        cache_subdir="nte",
        cache_name="NTE_setup.exe",
        error=NteLauncherError,
    )


def _tof_spec() -> LauncherSpec:
    """Tower of Fantasy's spec, built per call for the same reason as :func:`_nte_spec`."""
    return LauncherSpec(
        game="Tower of Fantasy",
        short="ToF",
        url=TOF_INSTALLER_URL,
        sha256=TOF_INSTALLER_SHA256,
        size=TOF_INSTALLER_SIZE,
        inner_archive=_INNER_ARCHIVE,
        payload_dir="HottaLaunch",
        destination="Launcher",
        exe="HottaLauncher.exe",
        cache_subdir="tof",
        cache_name="ToF_setup.exe",
        error=TofLauncherError,
    )


def _launcher_path(install_root: Path, spec: LauncherSpec) -> Path:
    """Return ``spec``'s launcher executable inside ``install_root``."""
    return install_root / spec.destination / spec.exe


def launcher_path(install_root: Path) -> Path:
    """Return NTE's launcher executable inside ``install_root``."""
    return _launcher_path(install_root, _nte_spec())


def tof_launcher_path(install_root: Path) -> Path:
    """Return Tower of Fantasy's launcher executable inside ``install_root``."""
    return _launcher_path(install_root, _tof_spec())


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 of ``path``."""
    from . import download

    return download.hash_file(path)["sha256"]


def _pin_is_valid(value: str) -> bool:
    """A pinned installer hash is exactly 64 lowercase hex digits."""
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _fetch_launcher_installer(
    cache: Path, download: Callable[[str, Path], Any], spec: LauncherSpec
) -> Path:
    """Download the installer into ``cache`` once, atomically, and verify its hash."""
    # The installer is downloaded and later run, so refuse a build with no usable pin before
    # asking the publisher for anything (#1593).
    if not _pin_is_valid(spec.sha256):
        raise spec.error(
            f"the {spec.short} installer has no valid checksum in this build; "
            "Nadare will not run an unpinned installer"
        )
    if cache.is_file():
        actual = _sha256(cache)
        if actual == spec.sha256:
            return cache
        cache.unlink(missing_ok=True)

    cache.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{spec.short}_setup-"
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".exe.part", dir=cache.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        download(spec.url, temporary)
        actual = _sha256(temporary)
        if actual != spec.sha256:
            raise spec.error(
                f"{cache.name} SHA-256 mismatch: expected {spec.sha256}, got {actual}"
            )
        os.replace(temporary, cache)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return cache


def _fetch_installer(cache: Path, download: Callable[[str, Path], Any]) -> Path:
    """Download and verify NTE's installer into ``cache``."""
    return _fetch_launcher_installer(cache, download, _nte_spec())


def _fetch_tof_installer(cache: Path, download: Callable[[str, Path], Any]) -> Path:
    """Download and verify Tower of Fantasy's installer into ``cache``."""
    return _fetch_launcher_installer(cache, download, _tof_spec())


def _extract(
    sevenz: str,
    archive: Path,
    outdir: Path,
    run: Callable[..., Any],
) -> Path:
    """Unpack ``archive`` into ``outdir``; the archive is always the last argument."""
    outdir.mkdir(parents=True, exist_ok=True)
    result = run(
        [sevenz, "x", "-y", f"-o{outdir}", str(archive)],
        env=process.child_environ(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NteLauncherError(
            f"cannot extract {archive.name} with {sevenz}: {result.stderr.strip()}"
        )
    return outdir


def _extract_for(
    spec: LauncherSpec, sevenz: str, archive: Path, outdir: Path, run: Callable[..., Any]
) -> Path:
    """Run :func:`_extract`, reporting a failure in ``spec``'s own error type."""
    try:
        return _extract(sevenz, archive, outdir, run)
    except NteLauncherError as exc:
        raise spec.error(str(exc)) from exc


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


def _ensure(
    spec: LauncherSpec,
    install_root: Path,
    *,
    data_dir: Path,
    download: Callable[[str, Path], Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    path_for: Callable[[Path], Path],
    fetch: Callable[[Path, Callable[[str, Path], Any]], Path],
) -> Path:
    """Make sure ``spec``'s launcher exists under ``install_root``.

    ``path_for`` and ``fetch`` are the game's public wrappers, so a monkeypatched
    ``launcher_path`` / ``_fetch_installer`` (or their ToF counterparts) still intercepts us.
    """
    installed = path_for(install_root)
    if installed.is_file():
        return installed

    sevenz = sevenzip.find(which=which)
    if not sevenz:
        raise spec.error(
            "7-Zip not found; install p7zip or 7zip so the "
            f"{spec.game} launcher can be extracted from the official installer"
        )

    if download is None:

        def _download(url: str, dest: Path) -> Any:
            return _dl.download(url, dest)

        download = _download

    cache = fetch(data_dir / spec.cache_subdir / spec.cache_name, download)

    with tempfile.TemporaryDirectory(prefix=f"{spec.short.lower()}-launcher-") as tmpdir:
        outer = _extract_for(spec, sevenz, cache, Path(tmpdir) / "installer", run)
        inner = next(
            (path for path in outer.rglob(spec.inner_archive) if path.is_file()), None
        )
        if inner is None:
            raise spec.error(f"the installer does not contain {spec.inner_archive}")

        payload = _extract_for(spec, sevenz, inner, Path(tmpdir) / "payload", run)
        launcher = next(
            (
                path
                for path in payload.rglob(spec.exe)
                if path.is_file()
                and path.parent.name == spec.payload_dir
                and path.parent.parent == payload
            ),
            None,
        )
        if launcher is None:
            raise spec.error(
                f"the installer payload does not contain the {spec.payload_dir} launcher "
                f"({spec.exe})"
            )

        _stage_tree(launcher.parent, install_root / spec.destination)

    return path_for(install_root)


def ensure_launcher(
    install_root: Path,
    *,
    data_dir: Path,
    download: Callable[[str, Path], Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Make sure NTE's publisher launcher exists under ``install_root``."""
    return _ensure(
        _nte_spec(),
        install_root,
        data_dir=data_dir,
        download=download,
        which=which,
        run=run,
        path_for=launcher_path,
        fetch=_fetch_installer,
    )


def ensure_tof_launcher(
    install_root: Path,
    *,
    data_dir: Path,
    download: Callable[[str, Path], Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Make sure Tower of Fantasy's publisher launcher exists under ``install_root``."""
    return _ensure(
        _tof_spec(),
        install_root,
        data_dir=data_dir,
        download=download,
        which=which,
        run=run,
        path_for=tof_launcher_path,
        fetch=_fetch_tof_installer,
    )
