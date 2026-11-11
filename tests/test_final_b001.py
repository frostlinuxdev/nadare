# SPDX-License-Identifier: GPL-3.0-or-later
"""A yostar sync that fails after replacing files must not claim nothing changed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core import yostar
from nadare.core.backends import BackendError


def _manifest(tmp_path: Path) -> Path:
    files = {"Arknights.exe": b"MZ", "data/big.pak": b"BIG"}
    payload = {
        "game_tag": "Arknights_EN",
        "version": "2.0",
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": p, "size": len(d), "crc64": yostar.crc64(d)} for p, d in files.items()
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_partial_sync_does_not_say_nothing_was_changed(tmp_path, monkeypatch):
    backend = ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"manifest_file": str(_manifest(tmp_path))})
    )
    write_exe(backend.direct_target(), "MZ")
    (backend.install_dir() / "data").mkdir(parents=True, exist_ok=True)

    def half(man, dest, **kw):
        (Path(dest) / "data" / "big.pak").write_bytes(b"N")  # a file was replaced
        raise yostar.ManifestError("crc64 mismatch for data/big.pak")

    monkeypatch.setattr(ak.yostar, "sync", half)
    with pytest.raises(BackendError) as caught:
        backend.update()

    message = str(caught.value)
    assert backend._install_marker_path().exists(), "a part-way sync must keep the marker"
    assert "Nothing was changed" not in message, message
