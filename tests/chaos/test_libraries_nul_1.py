# SPDX-License-Identifier: GPL-3.0-or-later
"""A damaged library entry must be ignored, as library_roots() documents.

A hand-edited or half-written config can contain a NUL in a path. That entry is damaged
input; it must not take down the whole startup path with a ValueError.
"""

from __future__ import annotations

from nadare.core import libraries
from nadare.core.config import Config


def test_a_library_path_with_a_nul_is_ignored(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    bad = str(tmp_path / "Games") + "\x00evil"
    cfg.data[libraries.CONFIG_KEY] = [{"path": bad}]

    try:
        roots = libraries.library_roots(cfg)
    except (ValueError, OSError) as exc:
        print("bad entry:", repr(bad))
        print("library_roots raised:", type(exc).__name__, exc)
        raise AssertionError(
            "a damaged library entry made library_roots() raise instead of being ignored"
        ) from exc

    print("roots:", roots)
    print("default:", libraries.default_root())
    assert roots == [libraries.default_root()]
