# SPDX-License-Identifier: GPL-3.0-or-later
"""A Sophon manifest file that is a folder on disk must be DirectError, not IsADirectoryError."""

from __future__ import annotations

import pytest
from _hostile_server import SophonBuild

from nadare.core import directdl as dd

DATA = b"GAME"


def test_a_sophon_file_cannot_replace_an_existing_folder(tmp_path):
    build = SophonBuild({"a.bin": DATA})
    server = build.serve()
    try:
        dest = tmp_path / "install"
        (dest / "a.bin").mkdir(parents=True)
        (dest / "a.bin" / "keep.txt").write_text("USER DATA")
        print("target before:", sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")))

        plan = dd.Plan("T", "1", sophon=(build.package(len(DATA)),))
        with pytest.raises(dd.DirectError):
            dd.sync(plan, dest, workers=1)

        assert (dest / "a.bin" / "keep.txt").read_text() == "USER DATA"
    finally:
        server.close()
