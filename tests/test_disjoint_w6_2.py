# SPDX-License-Identifier: GPL-3.0-or-later
"""W6-2: cancelling a Yostar update before any write leaves the game "incomplete"."""

from __future__ import annotations

import json

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.core import yostar
from nadare.core.events import Cancelled

FILES = {"Arknights.exe": b"MZ", "data/big.pak": b"BIG"}


def _manifest(tmp_path, version):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "game_tag": "Arknights_EN", "version": version,
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": n, "size": len(d), "crc64": yostar.crc64(d)} for n, d in FILES.items()
        ],
    }), encoding="utf-8")
    return path


def _write_all(directory):
    for name, data in FILES.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_a_cancel_before_the_first_write_keeps_the_game_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    backend = ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"manifest_file": str(_manifest(tmp_path, "1.0.0"))})
    )
    backend._ensure_default_runner = lambda **kw: None
    backend._ensure_direct_prefix = lambda: None
    monkeypatch.setattr(ak.yostar, "sync", lambda man, d, **kw: (
        _write_all(backend.install_dir()), {"downloaded": 2}
    )[1])
    backend.install()
    assert backend.status().state == "installed"

    # 1.1 is published.  Cancel at once, before the sync writes anything: like a
    # direct game, the untouched 1.0 install must stay usable.
    _manifest(tmp_path, "1.1.0")
    monkeypatch.setattr(ak.yostar, "sync", lambda man, d, **kw: (_ for _ in ()).throw(
        Cancelled("cancelled")
    ))
    with pytest.raises(Cancelled):
        backend.update()

    status = backend.status()
    marker = backend._install_marker_path()
    assert status.state == "installed", (
        f"a cancel before any write left state={status.state!r} message={status.message!r}; "
        f"incomplete marker_exists={marker.exists()}"
    )
