# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand-edited predownload record must not turn "false" into a ready notice."""

from __future__ import annotations

import json
import time

from _backend_helpers import make_context

from nadare.backends import direct


def test_a_string_false_in_predownload_json_is_not_ready(tmp_path):
    game = tmp_path / "game"
    state_dir = game / ".nadare"
    state_dir.mkdir(parents=True)
    (state_dir / "predownload.json").write_text(
        json.dumps(
            {
                "version": "3.4.0",
                "checked_at": int(time.time()),
                "ready": "false",
            }
        ),
        encoding="utf-8",
    )
    backend = direct.ZenlessBackend(
        make_context(
            tmp_path,
            "direct-zzz",
            {"install_dir": str(game), "prefix": str(tmp_path / "prefix")},
        )
    )

    state = backend._read_predownload_state()

    print("parsed predownload state:", state)
    assert state is not None
    assert state["ready"] is False
