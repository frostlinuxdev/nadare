# SPDX-License-Identifier: GPL-3.0-or-later
"""W6-1: launcherless NIKKE — Verify passes and Repair is refused for a corrupt client."""

from __future__ import annotations

import hashlib
import json

from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import NikkeBackend
from nadare.core.backends import BackendUnsupported


def test_a_corrupt_nadare_managed_nikke_client_fails_verify_and_can_be_repaired(tmp_path):
    prefix = tmp_path / "prefix"
    launcher = write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    assert launcher.is_file()
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": True},
        )
    )
    # Nadare downloaded the client itself (launcherless mode).  The published
    # copy is known; this one is damaged.
    good = b"MZ good client"
    exe = backend._client_exe()
    write_exe(exe, "MZ damaged")
    backend._client_latest_path().parent.mkdir(parents=True, exist_ok=True)
    backend._client_latest_path().write_text(
        json.dumps({"size": len(good), "md5": hashlib.md5(good).hexdigest(), "version": "1.2"}),
        encoding="utf-8",
    )
    assert backend._client_outdated(backend._read_client_latest() or None) is True

    report = backend.verify()
    problems: list[str] = []
    if report["verified"]:
        problems.append(
            f"Verify says {report['message']!r} while the Nadare-managed client is damaged "
            f"(checks={report['checks']})"
        )
    # Repair is the button for a damaged Nadare-managed client; it must be offered.
    backend._sync_direct = lambda: {}
    backend._missing_entries = lambda **kw: []
    try:
        backend.repair()
    except BackendUnsupported as exc:
        problems.append(f"Repair is refused: {exc}")

    assert not problems, "; ".join(problems)
