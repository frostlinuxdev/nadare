# SPDX-License-Identifier: GPL-3.0-or-later
"""An archive folder entry colliding with a user file must be DirectError, not FileExistsError."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from _hostile_server import ObjectServer

from nadare.core import directdl as dd


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_an_archive_folder_cannot_replace_an_existing_file(tmp_path):
    archive = make_zip({"a/user.bin": b"GAME", "game.exe": b"MZ"})
    server = ObjectServer({"/g.zip": archive})
    try:
        dest = tmp_path / "install"
        dest.mkdir()
        (dest / "a").write_text("USER FILE")
        print("existing file before:", (dest / "a").read_text())

        plan = dd.Plan(
            "T",
            "1",
            archives=(
                dd.Archive(
                    "g.zip",
                    dd.RemoteFile(
                        "g.zip",
                        server.url + "/g.zip",
                        len(archive),
                        md5=hashlib.md5(archive).hexdigest(),
                    ),
                ),
            ),
        )
        with pytest.raises(dd.DirectError):
            dd.sync(plan, dest)

        assert (dest / "a").read_text() == "USER FILE"
    finally:
        server.close()
