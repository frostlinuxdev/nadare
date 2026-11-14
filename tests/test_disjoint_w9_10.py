# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-10: CZN's runner name and core's runner version must describe the same build."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runners


def test_runner_version_is_read_the_same_way(tmp_path):
    runner = tmp_path / "runners" / "dwproton-11.0-15-x86_64"
    runner.mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    # Some builds ship a single-token version file instead of Proton's "<epoch> <tag>".
    (runner / "version").write_text("dwproton-11.0-15\n")
    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"runner": str(runner),
                                             "prefix": str(tmp_path / "pfx")})
    )
    core_name = runners.runner_version(runner)
    czn_name = backend.runner_name()
    print("core runner_version:", repr(core_name), "| CZN runner_name:", repr(czn_name))
    assert czn_name == core_name, (
        "CZN reads the version file differently and names a different build"
    )
