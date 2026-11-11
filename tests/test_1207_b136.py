# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b136: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

# repo/tests/test_core_yostar_mkdir.py
from __future__ import annotations

import pytest

from nadare.core import yostar


def test_a_file_blocking_a_manifest_folder_is_a_manifest_error(tmp_path):
    (tmp_path / "data").write_bytes(b"left over from an older build")
    payload = b"new"
    manifest = yostar.Manifest(
        game_tag="t",
        version="2",
        base_url="https://cdn.yo-star.com/assets",
        generated_at="",
        files=(yostar.ManifestFile("data/new.bin", len(payload), yostar.crc64(payload)),),
    )

    with pytest.raises(yostar.ManifestError):
        yostar.sync(manifest, tmp_path, workers=1)
