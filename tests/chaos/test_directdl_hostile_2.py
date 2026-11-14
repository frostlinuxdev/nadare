# SPDX-License-Identifier: GPL-3.0-or-later
"""A loose-file target that is already a folder must fail as DirectError, keeping the folder."""

from __future__ import annotations

import hashlib

import pytest
from _hostile_server import ObjectServer

from nadare.core import directdl as dd

BODY = b"GAME"


def test_a_loose_file_cannot_replace_an_existing_folder(tmp_path):
    server = ObjectServer({"/x": BODY})
    try:
        dest = tmp_path / "install"
        (dest / "a.bin").mkdir(parents=True)
        (dest / "a.bin" / "keep.txt").write_text("USER DATA")
        print("target before:", sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")))

        plan = dd.Plan(
            "T",
            "1",
            files=(
                dd.RemoteFile(
                    "a.bin", server.url + "/x", len(BODY), md5=hashlib.md5(BODY).hexdigest()
                ),
            ),
        )
        with pytest.raises(dd.DirectError):
            dd.sync(plan, dest, workers=1)

        assert (dest / "a.bin" / "keep.txt").read_text() == "USER DATA"
    finally:
        server.close()
