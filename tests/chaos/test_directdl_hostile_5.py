# SPDX-License-Identifier: GPL-3.0-or-later
"""A game archive may not write inside Nadare's own .nadare state folder."""

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


def test_an_archive_cannot_forge_a_done_record_in_the_state_folder(tmp_path):
    forged = b'{"game":"T","version":"1"}'
    archive = make_zip({"game.exe": b"MZ", ".nadare/forged.done": forged})
    server = ObjectServer({"/g.zip": archive})
    try:
        dest = tmp_path / "install"
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
        try:
            dd.sync(plan, dest)
        except dd.DirectError as exc:
            print("archive rejected:", exc)
        else:
            written = dest / dd.STATE_DIR / "forged.done"
            print("state file from archive:", written.read_bytes() if written.exists() else None)
            pytest.fail("an archive was allowed to write Nadare's own .nadare state")
    finally:
        server.close()
