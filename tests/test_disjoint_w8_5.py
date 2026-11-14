# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-5: CLI ``status`` must discover the same published update the GUI check finds."""

from __future__ import annotations

import json
from types import SimpleNamespace

from _backend_helpers import make_context, write_exe

from nadare import app
from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core import yostar


def test_cli_status_refreshes_a_stale_published_version(tmp_path, monkeypatch):
    tag = "Arknights_EN"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "game_tag": tag,
                "version": "3.8.0",
                "base_url": "https://cdn.yo-star.com/assets",
                "generated_at": "2026-01-01T00:00:00Z",
                "files": [
                    {"path": "Arknights.exe", "size": 2, "crc64": yostar.crc64(b"MZ")}
                ],
            }
        )
    )
    backend = ArknightsBackend(make_context(tmp_path, ak.KIND, {"manifest_file": str(manifest)}))
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file("3.7.2", tag)
    # The cache was written by the install long ago (older than the 6 h interval).
    path = backend._latest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "3.7.2", "checked_at": 0}))

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    events = []
    rc = app._run_backend_command(
        SimpleNamespace(command="status"),
        backend.ctx.config,
        "test",
        None,
        events.append,
    )
    data = events[-1].data
    print("rc:", rc, "| emitted data:", data)
    assert rc == 0
    assert data.get("update_available") is True, (
        "CLI status read the stale cache and never ran check_update; the GUI would say UPDATE"
    )
    assert data.get("latest_version") == "3.8.0", data.get("latest_version")
