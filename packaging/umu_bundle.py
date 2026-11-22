# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Read the modules inside the umu zipapp bundled for the AppImage (#1341 item 3).

PyInstaller cannot see imports that live inside a data file. When ``/usr/bin/python3`` is
missing, the hidden ``nadare --umu-run`` runs the zipapp with Nadare's own interpreter, so the
frozen build must carry every module the zipapp has. Reading them from the archive keeps the
list right when umu is updated (``scripts/fetch_umu.sh``).
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

#: The tar member that holds the zipapp (``core/umu.py`` ``_MEMBER``).
MEMBER = "umu/umu-run"
# Analyze the entry script under a unique name, without executing it at Nadare startup.
ENTRY_MODULE = "_nadare_umu_entry"


def _zipapp(archive: Path) -> bytes:
    with tarfile.open(archive, "r:*") as tar:
        source = tar.extractfile(MEMBER)
        if source is None:
            raise ValueError(f"the umu archive has no {MEMBER}")
        return source.read()


def modules(archive: Path) -> list[str]:
    """Every module, including a renamed entry script for PyInstaller's import analysis."""
    found: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(_zipapp(archive))) as app:
        for name in app.namelist():
            if not name.endswith(".py"):
                continue
            parts = name[: -len(".py")].split("/")
            if any(
                part.startswith(".") or part.endswith(".dist-info") or part == "__pycache__"
                for part in parts
            ):
                continue
            if parts == ["__main__"]:
                found.add(ENTRY_MODULE)
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts and all(part.isidentifier() for part in parts):
                found.add(".".join(parts))
    return sorted(found)


def extract(archive: Path, destination: Path) -> Path:
    """Unpack the zipapp so PyInstaller can find its modules (``pathex``)."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(_zipapp(archive))) as app:
        app.extractall(destination)
        (destination / f"{ENTRY_MODULE}.py").write_bytes(app.read("__main__.py"))
    return destination
