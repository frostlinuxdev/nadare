# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Unpack the Visual C++ runtime DLLs from the redistributable instead of running its installer.

``vc_redist.x64.exe`` is a WiX Burn bundle: a small UX cabinet, then a package cabinet that holds
the x64 DLLs in a nested cabinet as ``<name>.dll_amd64``. Its installer can idle forever under
Wine, but the DLLs inside are plain files, so copying them into ``system32`` gives Wine the same
real runtime. The caller passes a file it has already checked against the pinned hash; nothing
here runs code from the installer.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import process, sevenzip

log = logging.getLogger(__name__)

#: The x64 runtime DLLs the redistributable installs for 2015-2022 (MFC is left out: games do not
#: ship against it and it is 11 MB).
RUNTIME_DLLS: tuple[str, ...] = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcruntime140_threads.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "concrt140.dll",
    "vcamp140.dll",
    "vccorlib140.dll",
    "vcomp140.dll",
)
#: Without these two Nadare does not count the runtime as installed (see ``vcredist.installed``).
REQUIRED: tuple[str, ...] = ("vcruntime140.dll", "msvcp140.dll")

EXTRACT_TIMEOUT = 120.0
_CAB_MAGIC = b"MSCF\0\0\0\0"
_CAB_HEADER = 36
#: How deep cabinets may nest, how many are opened, and the biggest bundle read into memory.
_MAX_DEPTH = 3
_MAX_CABS = 24
_MAX_INSTALLER_BYTES = 200 * 1024 * 1024
_MAX_DLL_BYTES = 64 * 1024 * 1024
_X64 = 0x8664
_SUFFIX = "_amd64"


class UnpackError(Exception):
    """The runtime could not be unpacked; nothing was left half-done that matters."""


def _cabinets(data: bytes) -> list[tuple[int, int]]:
    """``(offset, size)`` of every cabinet in ``data``, skipping ones nested inside another."""
    found: list[tuple[int, int]] = []
    covered = 0
    start = 0
    while True:
        at = data.find(_CAB_MAGIC, start)
        if at < 0:
            return found
        start = at + 1
        if at < covered or at + _CAB_HEADER > len(data):
            continue
        size = struct.unpack_from("<I", data, at + 8)[0]
        if _CAB_HEADER < size <= len(data) - at:
            found.append((at, size))
            covered = at + size


def _run_7z(
    sevenz: str, cab: Path, outdir: Path, run: Callable[..., Any], installer_name: str
) -> None:
    try:
        result = run(
            [sevenz, "x", "-tCab", "-y", "-bd", f"-o{outdir}", str(cab)],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnpackError(f"unpacking {installer_name} took too long") from exc
    except OSError as exc:
        raise UnpackError(f"cannot run 7-Zip: {exc}") from exc
    if result.returncode not in (0, 1):  # 1 = warnings only
        raise UnpackError(f"7-Zip could not read part of {installer_name}")


def _collect(
    sevenz: str, installer: Path, scratch: Path, run: Callable[..., Any]
) -> dict[str, Path]:
    """Every wanted ``*.dll_amd64`` found in the installer's cabinets (and theirs)."""
    wanted = {f"{n}{_SUFFIX}": n for n in RUNTIME_DLLS}
    try:
        size = installer.stat().st_size
    except OSError as exc:
        raise UnpackError(f"cannot read {installer.name}: {exc}") from exc
    if size > _MAX_INSTALLER_BYTES:
        raise UnpackError(f"{installer.name} is unexpectedly large")
    found: dict[str, Path] = {}
    queue: list[tuple[Path, int]] = [(installer, 0)]
    opened = 0
    while queue:
        source, depth = queue.pop(0)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise UnpackError(f"cannot read {source.name}: {exc}") from exc
        for at, length in _cabinets(data):
            opened += 1
            if opened > _MAX_CABS:
                return found
            cab = scratch / f"cab{opened}.cab"
            cab.write_bytes(data[at : at + length])
            out = scratch / f"out{opened}"
            out.mkdir()
            _run_7z(sevenz, cab, out, run, installer.name)
            for path in sorted(out.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                name = wanted.get(path.name.lower())
                if name is not None:
                    found.setdefault(name, path)
                elif depth + 1 < _MAX_DEPTH and path.stat().st_size > _CAB_HEADER:
                    with open(path, "rb") as f:
                        if f.read(8) == _CAB_MAGIC:
                            queue.append((path, depth + 1))
    return found


def _is_x64_pe(path: Path) -> bool:
    try:
        if path.stat().st_size > _MAX_DLL_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if len(head) < 0x40 or head[:2] != b"MZ":
        return False
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 6 > len(head) or head[pe : pe + 4] != b"PE\0\0":
        return False
    return struct.unpack_from("<H", head, pe + 4)[0] == _X64


def _is_placeholder(path: Path) -> bool:
    """A Wine builtin stand-in (or a symlink or an unreadable file): fair game to replace."""
    if path.is_symlink():
        return True
    try:
        with open(path, "rb") as f:
            return b"Wine builtin DLL" in f.read(4096)
    except OSError:
        return True


def _place(source: Path, dest: Path) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=dest.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out)
        temp.chmod(0o644)
        os.replace(temp, dest)  # replaces a planted symlink itself, never what it points to
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def unpack(
    installer: Path, prefix: Path, *, run: Callable[..., Any] = subprocess.run
) -> list[str]:
    """Copy the x64 runtime DLLs from ``installer`` into ``prefix``; return the names written.

    A DLL that is already a real (non-placeholder) file is kept. Every DLL is checked to be an x64
    PE before any is copied, and each copy is atomic.
    """
    installer = Path(installer)
    system32 = Path(prefix) / "drive_c" / "windows" / "system32"
    if not system32.is_dir():
        raise UnpackError("the prefix has no Windows folder yet")
    sevenz = sevenzip.find()
    if not sevenz:
        raise UnpackError("7-Zip is not available")
    scratch = Path(tempfile.mkdtemp(prefix=".vcunpack-"))
    try:
        found = _collect(sevenz, installer, scratch, run)
        for name in REQUIRED:
            if name not in found:
                raise UnpackError(f"{name} was not found inside {installer.name}")
        for name, path in found.items():
            if not _is_x64_pe(path):
                raise UnpackError(f"{name} in {installer.name} is not an x64 Windows DLL")
        copied: list[str] = []
        for name in RUNTIME_DLLS:
            if name not in found:
                continue
            dest = system32 / name
            if dest.exists() and not _is_placeholder(dest):
                continue
            try:
                _place(found[name], dest)
            except OSError as exc:
                raise UnpackError(f"cannot write {name}: {exc}") from exc
            copied.append(name)
        log.info("unpacked %d VC++ runtime DLL(s) into %s", len(copied), system32)
        return copied
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_FIXED_FILE_INFO = struct.pack("<I", 0xFEEF04BD)  # VS_FIXEDFILEINFO signature
_VERSION_SCAN_BYTES = 8 * 1024 * 1024


def runtime_version(prefix: Path) -> tuple[int, int, int, int] | None:
    """The file version of the prefix's ``vcruntime140.dll`` (e.g. ``(14, 44, 35211, 0)``).

    The real redistributable's registry values are this very version, so reading it from the DLL
    keeps them right whichever build Microsoft ships. ``None`` when it cannot be read.
    """
    path = Path(prefix) / "drive_c" / "windows" / "system32" / "vcruntime140.dll"
    try:
        with open(path, "rb") as f:
            data = f.read(_VERSION_SCAN_BYTES)
    except OSError:
        return None
    at = data.find(_FIXED_FILE_INFO)
    while at >= 0:
        if at + 16 <= len(data):
            _sig, structure, ms, ls = struct.unpack_from("<IIII", data, at)
            if structure == 0x00010000 and ms:
                return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF
        at = data.find(_FIXED_FILE_INFO, at + 1)
    return None


def registry_text(version: tuple[int, int, int, int]) -> str:
    """A ``.reg`` file with the keys the real installer writes for the x64 runtime.

    ``Runtimes\\x64`` is what installers and launchers read to decide the runtime is there; the
    installer writes it in both the 64-bit and the Wow6432Node view.
    """
    major, minor, build, rev = version
    values = (
        f'"Bld"=dword:{build:08x}\n'
        '"Installed"=dword:00000001\n'
        f'"Major"=dword:{major:08x}\n'
        f'"Minor"=dword:{minor:08x}\n'
        f'"Rbld"=dword:{rev:08x}\n'
        f'"Version"="v{major}.{minor}.{build}.{rev:02d}"\n'
    )
    tail = "Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64]"
    return (
        "Windows Registry Editor Version 5.00\n\n"
        f"[HKEY_LOCAL_MACHINE\\SOFTWARE\\{tail}\n{values}\n"
        f"[HKEY_LOCAL_MACHINE\\SOFTWARE\\Wow6432Node\\{tail}\n{values}"
    )


__all__ = ["REQUIRED", "RUNTIME_DLLS", "UnpackError", "registry_text", "runtime_version", "unpack"]
