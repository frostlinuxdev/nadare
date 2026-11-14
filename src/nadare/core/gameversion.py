# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and compare the versions a game records and publishes.

The install record (``.nadare-version.json``) is shared by status, the support report and
Find games; CZN keeps its version in the game's UPF instead. Keeping the readers and the
``+client-`` comparison here means every surface answers with the same version rule.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

#: Files an install leaves in its install folder. Core cannot import a backend's constants
#: (tests/test_layering.py), so they live here and the backends import them.
VERSION_FILE = ".nadare-version.json"
INCOMPLETE_MARKER = ".nadare-install-incomplete"
VERSION_MAX_BYTES = 64 * 1024

#: CZN's version record: the UPF has no file extension and its service name is fixed.
CZN_GAME_SUBDIR = Path("drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare")
CZN_SERVICE = "STOVE_CHAOSZERO"


def read_version_record(install_dir: Path | str | None) -> dict[str, Any] | None:
    """The install's version record, or ``None`` when absent/unreadable/malformed."""
    if install_dir is None:
        return None
    try:
        fd = os.open(
            Path(install_dir) / VERSION_FILE,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError:
        return None
    try:
        # A FIFO would block a plain read; only a regular file can hold the JSON.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, VERSION_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > VERSION_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def read_install_version(install_dir: Path | str | None) -> str | None:
    """The version recorded by the last successful direct sync, or ``None``."""
    data = read_version_record(install_dir)
    value = data.get("version") if data else None
    return value if isinstance(value, str) and value else None


def read_czn_version(prefix: Path | str | None) -> str | None:
    """CZN's installed version from its UPF, the same field its status shows."""
    if prefix is None:
        return None
    upf = (
        Path(prefix)
        / CZN_GAME_SUBDIR
        / "combinedata_manifest"
        / f"GameManifest_{CZN_SERVICE}.upf"
    )
    try:
        data = json.loads(upf.read_text(encoding="utf-8"))
        value = int(data["local_version"])
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, RecursionError):
        return None
    return str(value)


def versions_equal(recorded: object, current: str) -> bool:
    """Whether an install recorded at ``recorded`` is current for ``current``.

    A plan version may carry a ``+client-<version>`` suffix that older installs never recorded
    (#866). The archive's URL, which names that client version, is compared separately, so a
    record made before the suffix existed still counts when everything else matches.
    """
    if recorded == current:
        return True
    base, sep, _client = current.partition("+client-")
    return bool(sep) and recorded == base


__all__ = [
    "CZN_GAME_SUBDIR",
    "CZN_SERVICE",
    "INCOMPLETE_MARKER",
    "VERSION_FILE",
    "VERSION_MAX_BYTES",
    "read_czn_version",
    "read_install_version",
    "read_version_record",
    "versions_equal",
]
