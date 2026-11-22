# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""CRC-64/XZ, the whole-file checksum in Yostar's manifests (shared by ``yostar`` and ``directdl``).

Moved out of :mod:`nadare.core.yostar` for #1452 so direct downloads can check it without
importing the Yostar engine.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import threading
from typing import Any

# ---------------------------------------------------------------------------
# CRC-64/XZ (reflected ECMA-182).  liblzma exposes this as ``lzma_crc64`` and
# runs orders of magnitude faster than a pure-Python byte loop, so prefer it
# when present and fall back to the table below otherwise.  The native call
# takes the running CRC directly as its third argument, matching the
# incremental contract of :func:`crc64` (``crc64(b, crc64(a)) == crc64(a+b)``).
# ---------------------------------------------------------------------------
_CRC64_POLY = 0xC96C5795D7870F42
_CRC64_MASK = 0xFFFFFFFFFFFFFFFF
_CRC64_TABLE: list[int] = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ _CRC64_POLY if (_crc & 1) else _crc >> 1
    _CRC64_TABLE.append(_crc)
del _i, _crc


def _load_native_crc64() -> Any:
    """Return liblzma's ``lzma_crc64`` as a ctypes function, or ``None``."""
    names: list[str] = []
    try:
        found = ctypes.util.find_library("lzma")
    except OSError:
        found = None
    if found:
        names.append(found)
    names += ["liblzma.so.5", "liblzma.so"]
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            fn = lib.lzma_crc64
        except (OSError, AttributeError):
            continue
        fn.restype = ctypes.c_uint64
        fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint64]
        return fn
    return None


_NATIVE_CRC64 = _load_native_crc64()
_fallback_logged = False
_fallback_lock = threading.Lock()


def _crc64_python(data: bytes, crc: int = 0) -> int:
    value = (crc ^ _CRC64_MASK) & _CRC64_MASK
    table = _CRC64_TABLE
    for byte in data:
        value = (value >> 8) ^ table[(value ^ byte) & 0xFF]
    return (value ^ _CRC64_MASK) & _CRC64_MASK


def _crc64_native(data: bytes, crc: int = 0) -> int:
    if _NATIVE_CRC64 is None:
        raise RuntimeError("liblzma crc64 is unavailable")
    if not isinstance(data, bytes):
        data = bytes(data)
    return int(_NATIVE_CRC64(data, len(data), crc & _CRC64_MASK))


def crc64(data: bytes, crc: int = 0) -> int:
    """Return the CRC-64/XZ of ``data``, continuing from ``crc``."""
    if _NATIVE_CRC64 is not None:
        return _crc64_native(data, crc)
    global _fallback_logged
    with _fallback_lock:
        if not _fallback_logged:
            logging.getLogger(__name__).warning(
                "CRC-64 uses pure Python; install liblzma for faster checks"
            )
            _fallback_logged = True
    return _crc64_python(data, crc)


class Crc64:
    """Incremental CRC-64/XZ helper: ``update(block)`` then read ``value``."""

    __slots__ = ("_value",)

    def __init__(self, crc: int = 0) -> None:
        self._value = crc & _CRC64_MASK

    def update(self, data: bytes) -> Crc64:
        self._value = crc64(data, self._value)
        return self

    def reset(self) -> None:
        self._value = 0

    @property
    def value(self) -> int:
        return self._value


def crc64_file(path: str | os.PathLike[str], *, chunk: int = 1 << 20) -> int:
    """Stream ``path`` and return its CRC-64/XZ (native when available)."""
    hasher = Crc64()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.value


def normalize_crc64(value: int | str | None) -> int:
    """Normalise a decimal/hex CRC-64 (with or without ``0x``) to an int."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("invalid CRC-64")
    if isinstance(value, int):
        return value & _CRC64_MASK
    text = str(value).strip()
    if not text:
        return 0
    if text.lower().startswith("0x"):
        return int(text, 16) & _CRC64_MASK
    digits = text.lstrip("+-")
    if digits.isdigit():
        return int(text, 10) & _CRC64_MASK
    if re.fullmatch(r"[0-9a-fA-F]+", text):
        return int(text, 16) & _CRC64_MASK
    raise ValueError(f"invalid CRC-64: {value!r}")
