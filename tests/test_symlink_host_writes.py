# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side writes must not follow a planted symlink out of the install folder (#963, #964)."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from _backend_helpers import make_context
from _httpserver import Server
from test_direct_backend import _wuwa

from nadare.backends import arknights as ak
from nadare.core import directdl as dd


def _zip_blob() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("game.exe", b"good")
    return buf.getvalue()


def test_a_symlinked_cache_folder_does_not_redirect_the_download(tmp_path):
    blob = _zip_blob()
    server = Server({"/g.zip": blob})
    try:
        root = tmp_path / "install"
        root.mkdir()
        outside = tmp_path / "personal"
        outside.mkdir()
        victim = outside / "archive.zip"
        victim.write_bytes(b"personal file")
        (root / dd.DL_DIR).mkdir()
        (root / dd.DL_DIR / "nested").symlink_to(outside, target_is_directory=True)
        archive = dd.Archive(
            "game",
            dd.RemoteFile(
                "nested/archive.zip",
                server.url + "/g.zip",
                len(blob),
                md5=hashlib.md5(blob).hexdigest(),
            ),
        )
        with pytest.raises(dd.DirectError, match="symlink"):
            dd.sync(dd.Plan("T", "1", archives=(archive,)), root)
        assert victim.read_bytes() == b"personal file"
        assert not (root / "game.exe").exists()
    finally:
        server.close()


def test_a_symlinked_cache_file_is_refused_too(tmp_path):
    blob = _zip_blob()
    server = Server({"/g.zip": blob})
    try:
        root = tmp_path / "install"
        root.mkdir()
        victim = tmp_path / "personal.zip"
        victim.write_bytes(b"personal file")
        (root / dd.DL_DIR).mkdir()
        (root / dd.DL_DIR / "archive.zip").symlink_to(victim)
        archive = dd.Archive(
            "game",
            dd.RemoteFile(
                "archive.zip", server.url + "/g.zip", len(blob), md5=hashlib.md5(blob).hexdigest()
            ),
        )
        with pytest.raises(dd.DirectError, match="symlink"):
            dd.sync(dd.Plan("T", "1", archives=(archive,)), root)
        assert victim.read_bytes() == b"personal file"
    finally:
        server.close()


def test_a_symlinked_install_marker_is_replaced_not_followed(tmp_path):
    backend, _root, _game = _wuwa(tmp_path / "profile")
    victim = tmp_path / "personal.txt"
    victim.write_text("personal content must survive")
    backend._install_marker_path().symlink_to(victim)
    backend._load_manifest = lambda: dd.Plan("wuwa", "1")
    backend._refuse_if_running = lambda *a: None
    backend._sync_direct()
    assert victim.read_text() == "personal content must survive"


def test_the_shared_marker_writer_replaces_a_link_for_the_yostar_backend_too(tmp_path):
    backend = ak.ArknightsBackend(make_context(tmp_path, ak.KIND, None, "t"))
    marker = backend._install_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "personal.txt"
    victim.write_text("personal content must survive")
    marker.symlink_to(victim)
    backend._write_install_marker()
    assert victim.read_text() == "personal content must survive"
    assert marker.is_file() and not marker.is_symlink()
    assert marker.read_text() == "sync in progress\n"
