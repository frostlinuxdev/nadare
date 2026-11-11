# repo/tests/test_core_yostar_keeps_part.py
from __future__ import annotations

import pytest

from nadare.core import download as download_mod
from nadare.core import yostar


def test_a_transient_network_failure_keeps_the_resumable_part(tmp_path, monkeypatch):
    payload = b"0123456789abcdef"
    part = tmp_path / "big.bin.part"
    part.write_bytes(payload[:8])
    manifest = yostar.Manifest(
        game_tag="t",
        version="1",
        base_url="https://cdn.yo-star.com/assets",
        generated_at="",
        files=(
            yostar.ManifestFile("big.bin", len(payload), yostar.crc64(payload)),
        ),
    )

    def failing_stream(url, dest, **kwargs):
        raise download_mod.DownloadError("connection reset by peer")

    monkeypatch.setattr(yostar.download, "stream_download", failing_stream)
    with pytest.raises(yostar.ManifestError):
        yostar.sync(manifest, tmp_path, workers=1, retries=1)

    assert part.exists(), "the resumable .part was deleted after a network error"
