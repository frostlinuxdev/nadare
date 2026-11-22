# SPDX-License-Identifier: GPL-3.0-or-later
"""Real process argv stays literal and profile launch paths stay pinned."""

from __future__ import annotations

import json
import sys

import pytest

from nadare.core import process


@pytest.mark.parametrize(
    "argument",
    [
        "space name",
        "ゲーム",
        "semi;colon",
        "$(echo fixture)",
        "`echo fixture`",
        "a|b",
        "a&&b",
        "a>b",
        "--",
        'a"b',
        "a\\b",
        "line\nbreak",
        "",
        "%command%",
    ],
)
def test_arguments_reach_child_literally(tmp_path, argument):
    output = tmp_path / "argv.json"
    script = (
        "import sys,json\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))"
    )
    process.run([sys.executable, "-c", script, str(output), argument])
    assert json.loads(output.read_text()) == [argument]


@pytest.mark.parametrize("key", ["WINEPREFIX", "STEAM_COMPAT_DATA_PATH", "PROTONPATH"])
@pytest.mark.parametrize("value", ["/wrong/place", "", " space path "])
def test_extra_env_cannot_redirect_profile_launch(tmp_path, key, value):
    profile = {"prefix": str(tmp_path / "prefix"), "runner": str(tmp_path / "runner")}
    base = {"PATH": "/usr/bin:/bin", "KEEP": "yes"}
    old = dict(base)
    env = process.build_env(profile, extra={key: value, "VISIBLE": "yes"}, base=base)
    assert env["WINEPREFIX"] == profile["prefix"]
    assert env["PROTONPATH"] == profile["runner"]
    assert env.get("STEAM_COMPAT_DATA_PATH") != value
    assert env["VISIBLE"] == "yes" and base == old
