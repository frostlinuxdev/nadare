# SPDX-License-Identifier: GPL-3.0-or-later
# repo/tests/test_core_yostar_infinite_size.py
from __future__ import annotations

import pytest

from nadare.core import yostar


def test_an_infinite_size_is_a_manifest_error(tmp_path):
    raw = (
        b'{"game_tag":"t","version":"1",'
        b'"base_url":"https://cdn.yo-star.com/a",'
        b'"files":[{"path":"a","size":1e400,"crc64":0}]}'
    )
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)

    with pytest.raises(yostar.ManifestError):
        yostar.load_manifest(str(path))
