#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild installed PyInstaller bootloaders from the existing uv.lock sdist.

No dependency resolution or package installation: the release container already
syncs the locked environment. Verify the sdist hash and installed version, run
waf with PIE/full RELRO, and replace only the Linux x86_64 console bootloaders.
NADARE_PYINSTALLER_SOURCE can supply an offline copy of the locked sdist.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from check_elf_hardening import inspect_elf


def locked_source(lock: Path) -> tuple[str, str, str]:
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    (package,) = (p for p in packages if p["name"] == "pyinstaller")
    source = package["sdist"]
    algorithm, digest = source["hash"].split(":", 1)
    if algorithm != "sha256" or len(digest) != 64:
        raise ValueError("PyInstaller sdist must have a locked SHA-256")
    return package["version"], source["url"], digest


def extract_verified(archive: Path, digest: str, destination: Path) -> None:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("PyInstaller sdist SHA-256 mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(destination, filter="data")


def compile_bootloaders(source: Path, installed: Path) -> None:
    env = {
        **os.environ,
        "CFLAGS": "-fPIE",
        "LDFLAGS": "-pie -Wl,-z,relro,-z,now",
        # The release container has no zlib headers; PyInstaller then builds the zlib it ships
        # (its documented --static-zlib), so the container needs no extra package.
        "PYI_STATIC_ZLIB": "1",
    }
    subprocess.run(
        [sys.executable, "./waf", "distclean", "all"],
        cwd=source / "bootloader",
        env=env,
        check=True,
    )
    relative = Path("bootloader/Linux-64bit-intel")
    outputs = [source / "PyInstaller" / relative / name for name in ("run", "run_d")]
    # Verify both before replacing either. No exception applies to bootloaders.
    for output in outputs:
        if not all(inspect_elf(output.read_bytes()).values()):
            raise ValueError(f"bootloader hardening check failed: {output}")
    target = installed / relative
    target.mkdir(parents=True, exist_ok=True)
    for output in outputs:
        shutil.copy2(output, target / output.name)
    print("[*] Installed locked PyInstaller bootloaders: PIE, full RELRO, NX stack")


def main() -> None:
    import PyInstaller

    root = Path(__file__).resolve().parents[1]
    version, url, digest = locked_source(root / "uv.lock")
    if PyInstaller.__version__ != version:
        raise ValueError(f"installed PyInstaller must match uv.lock ({version})")
    with tempfile.TemporaryDirectory(prefix="pyinstaller-hardening-") as directory:
        work = Path(directory)
        local = os.environ.get("NADARE_PYINSTALLER_SOURCE")
        archive = Path(local) if local else work / "source.tar.gz"
        if not local:
            urllib.request.urlretrieve(url, archive)
        unpacked = work / "source"
        extract_verified(archive, digest, unpacked)
        source = unpacked / f"pyinstaller-{version}"
        compile_bootloaders(source, Path(PyInstaller.__file__).resolve().parent)


if __name__ == "__main__":
    main()
