# SPDX-License-Identifier: GPL-3.0-or-later
"""Damaged library entries are documented as ignored, including paths a NUL makes unusable."""

from __future__ import annotations

import json

from nadare.core import libraries
from nadare.core.config import Config


def test_a_library_path_with_nul_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "config.json"
    raw = {"profiles": {}, "libraries": [{"path": "/tmp/a\x00b"}]}
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = Config.load(path)
    print("stored libraries:", repr(config.data["libraries"]))

    roots = libraries.library_roots(config)

    print("library roots:", roots)
    assert roots == [libraries.default_root()]
