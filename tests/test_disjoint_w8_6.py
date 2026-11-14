# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-6: a launcher-only NIKKE update must not be labelled with the current client version."""

from __future__ import annotations

import hashlib
import json

from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import NikkeBackend


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_launcher_only_update_names_the_launcher_version(tmp_path):
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c/NIKKE/Launcher/nikke_launcher.exe", "MZ")
    exe = prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"current client")
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "launcherless": True},
        )
    )
    # Client is current (154.0.0); one launcher key file differs; launcher is 155.0.0.
    _write_json(
        backend._client_latest_path(),
        {
            "size": exe.stat().st_size,
            "md5": hashlib.md5(exe.read_bytes()).hexdigest(),
            "version": "154.0.0",
            "version_name": "NIKKE.PC_Offcial_GL_154.0.0",
            "checked_at": 1,
        },
    )
    _write_json(
        backend._launcher_latest_path(),
        {
            "files": {"nikke_launcher.exe": {"size": 99, "md5": "00" * 16}},
            "version": "155.0.0",
            "checked_at": 1,
        },
    )

    status = backend.status()
    print("update_available:", status.data.get("update_available"),
          "| latest_version:", status.data.get("latest_version"))
    assert status.data.get("update_available") is True, "fixture: launcher is outdated"
    assert status.data.get("latest_version") == "155.0.0", (
        "A launcher-only update is named with the already-current client version"
    )
