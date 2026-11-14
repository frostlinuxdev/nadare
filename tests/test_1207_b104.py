# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from nadare.core import sophon


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_bytes(num, value):
    raw = value.encode() if isinstance(value, str) else value
    return _varint((num << 3) | 2) + _varint(len(raw)) + raw


def _field_varint(num, value):
    return _varint(num << 3) + _varint(value)


def test_a_nul_in_a_manifest_path_is_a_sophon_error(tmp_path, monkeypatch):
    manifest = _field_bytes(
        1, _field_bytes(1, "a\x00b") + _field_varint(4, 0) + _field_bytes(5, "")
    )
    pkg = sophon.SophonPackage("game", "https://x/m", "https://x/c", "m1", 10)
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: b"x")
    monkeypatch.setattr(sophon, "_unzstd", lambda body, limit, what: manifest)
    with pytest.raises(sophon.SophonError):
        sophon.sync_package(pkg, tmp_path)
