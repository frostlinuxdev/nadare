# SPDX-License-Identifier: GPL-3.0-or-later
"""Small real-layout Proton trees shared by runner tests."""

from pathlib import Path


def make_runner(path: Path, *, lib: str = "lib", version: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "proton").write_text("#!/bin/sh\n")
    wine = path / "files" / "bin" / "wine"
    wine.parent.mkdir(parents=True, exist_ok=True)
    wine.write_text("#!/bin/sh\n")
    windows = path / "files" / lib / "wine" / "x86_64-windows"
    windows.mkdir(parents=True, exist_ok=True)
    for dll in ("ntdll.dll", "kernel32.dll", "kernelbase.dll"):
        (windows / dll).write_bytes(b"MZ")
    if version is not None:
        (path / "version").write_text(version + "\n")
    return path
