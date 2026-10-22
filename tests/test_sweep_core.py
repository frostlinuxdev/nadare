# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the core sweep fixes (#243, #242)."""

from __future__ import annotations

import pytest

from nadare.core import shortcuts

_HEADER = b"\x00shortcuts\x00"
_DEEP = _HEADER + b"\x00a\x00" * 5000 + b"\x08" * 5000


# ---------------------------------------------------------------------------
# #243 — a deeply nested/corrupt shortcuts.vdf is corruption, not a crash.
# ---------------------------------------------------------------------------
def test_vdf_loads_deep_nesting_is_corruption_not_recursion_error() -> None:
    assert shortcuts.vdf_loads(_DEEP) == []
    with pytest.raises(ValueError, match="corrupt shortcuts.vdf"):
        shortcuts.vdf_loads(_DEEP, strict=True)


def test_add_steam_shortcut_refuses_a_deeply_nested_vdf(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    vdf.write_bytes(_DEEP)

    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.add_steam_shortcut(vdf, "p", "T", ["/opt/n"])

    assert vdf.read_bytes() == _DEEP
