# SPDX-License-Identifier: GPL-3.0-or-later
"""#1826: ``changed`` means a file in the install was written, not "a step that may write began".

A failure before the first write (the CDN unreachable, a broken archive caught in staging, a
Sophon manifest that cannot be fetched) must leave ``changed`` clear, so a complete game stays
playable instead of showing RESUME. A sync that does write still sets it.
"""

from __future__ import annotations

import threading

import pytest
from test_directdl_stream import files, make_zip, server  # noqa: F401 - fixture reuse
from test_directdl_stream import plan as stream_plan
from test_sophon import Build, plan_for, servers  # noqa: F401 - fixture reuse

from nadare.core import directdl as dd
from nadare.core import sophon


def _zip_plan(url, size=None):
    src = dd.RemoteFile("g.zip", url, size)
    return dd.Plan("T", "1", archives=(dd.Archive("g", src, unwrap=("zip",)),))


def _installed(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "game.exe").write_bytes(b"MZ old")
    return {p.name: p.read_bytes() for p in root.iterdir()}


def test_a_streamed_pack_that_cannot_be_fetched_changes_nothing(server, tmp_path):  # noqa: F811
    s = server({})  # every request is a 404
    root = tmp_path / "game"
    before = _installed(root)
    changed = threading.Event()
    with pytest.raises(dd.DirectError):
        dd.sync(stream_plan(s.url + "/g.zip", size=1000), root, workers=1, changed=changed)
    assert not changed.is_set()
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_a_streamed_pack_that_installs_sets_changed(server, tmp_path):  # noqa: F811
    z = make_zip(files(3))
    s = server({"/g.zip": z})
    changed = threading.Event()
    dd.sync(stream_plan(s.url + "/g.zip", size=len(z)), tmp_path, workers=1, changed=changed)
    assert changed.is_set()


def test_a_downloaded_pack_that_is_broken_changes_nothing(server, tmp_path):  # noqa: F811
    s = server({"/g.zip": b"this is not a zip" * 100})
    root = tmp_path / "game"
    _installed(root)
    changed = threading.Event()
    with pytest.raises(dd.DirectError):
        dd.sync(_zip_plan(s.url + "/g.zip"), root, workers=1, changed=changed)
    assert not changed.is_set()


def test_a_downloaded_pack_that_installs_sets_changed(server, tmp_path):  # noqa: F811
    z = make_zip(files(3))
    s = server({"/g.zip": z})
    changed = threading.Event()
    dd.sync(_zip_plan(s.url + "/g.zip", len(z)), tmp_path, workers=1, changed=changed)
    assert changed.is_set()


def test_a_sophon_manifest_that_cannot_be_fetched_changes_nothing(tmp_path):
    pkg = sophon.SophonPackage(
        name="game",
        manifest_url="http://127.0.0.1:9/m",
        chunk_url="http://127.0.0.1:9/c",
        manifest_id="m1",
        size=100,
    )
    root = tmp_path / "game"
    _installed(root)
    changed = threading.Event()
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", sophon=(pkg,)), root, workers=1, changed=changed)
    assert not changed.is_set()


def test_a_sophon_install_sets_changed(servers, tmp_path):  # noqa: F811
    build = Build({"a.bin": b"a" * 100}, chunk_size=100)
    servers(build)
    changed = threading.Event()
    dd.sync(plan_for(build), tmp_path, workers=1, changed=changed)
    assert changed.is_set()


def test_a_sophon_sync_with_nothing_to_write_changes_nothing(servers, tmp_path):  # noqa: F811
    build = Build({"a.bin": b"a" * 100}, chunk_size=100)
    servers(build)
    dd.sync(plan_for(build), tmp_path, workers=1)
    changed = threading.Event()
    dd.sync(plan_for(build), tmp_path, workers=1, changed=changed)
    assert not changed.is_set()
