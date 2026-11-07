# SPDX-License-Identifier: GPL-3.0-or-later
"""A completion record that lists only part of an archive is not proof it is all there (#906)."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from _httpserver import Server as _Server

from nadare.core import directdl as dd


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def served():
    made = []

    def make(objects):
        server = _Server(objects)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _plan(server, blob):
    archive = dd.Archive(
        "g",
        dd.RemoteFile("g.zip", server.url + "/g.zip", len(blob), md5=hashlib.md5(blob).hexdigest()),
    )
    return dd.Plan("T", "1", archives=(archive,))


def test_a_file_missing_outside_a_truncated_record_is_noticed(served, tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_MAX_RECORDED_MEMBERS", 2)
    blob = _zip({"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    plan = _plan(served({"/g.zip": blob}), blob)
    dd.sync(plan, tmp_path)
    assert dd.plan_missing(plan, tmp_path) == ([], [])  # complete: nothing to do
    (tmp_path / "c.bin").unlink()  # the third file is outside the recorded two
    _files, archives = dd.plan_missing(plan, tmp_path)
    assert [a.name for a in archives] == ["g"]


def test_a_complete_install_with_a_truncated_record_is_not_fetched_again(
    served, tmp_path, monkeypatch
):
    monkeypatch.setattr(dd, "_MAX_RECORDED_MEMBERS", 2)
    blob = _zip({"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    server = served({"/g.zip": blob})
    plan = _plan(server, blob)
    dd.sync(plan, tmp_path)
    requests = len(server.requests)
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(server.requests) == requests


def test_a_truncated_record_without_its_member_list_is_not_trusted(served, tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_MAX_RECORDED_MEMBERS", 2)
    blob = _zip({"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    plan = _plan(served({"/g.zip": blob}), blob)
    dd.sync(plan, tmp_path)
    for sidecar in (tmp_path / dd.STATE_DIR).glob("*.members"):
        sidecar.unlink()
    _files, archives = dd.plan_missing(plan, tmp_path)
    assert [a.name for a in archives] == ["g"]


def test_an_untruncated_record_still_counts_as_done(served, tmp_path):
    blob = _zip({"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    server = served({"/g.zip": blob})
    plan = _plan(server, blob)
    dd.sync(plan, tmp_path)
    requests = len(server.requests)
    assert dd.plan_missing(plan, tmp_path) == ([], [])
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(server.requests) == requests
