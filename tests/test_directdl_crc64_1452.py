# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 1: direct downloads check Yostar's whole-file CRC-64 (CRC-64/XZ).

Yostar manifests list a size and a CRC-64 per file; once Arknights and Stella Sora run on the
shared direct-download engine, that engine has to check it the way ``yostar.sync`` does.
"""

from __future__ import annotations

import pytest
from test_directdl_prune import server  # noqa: F401 - fixture reuse

from nadare.core import crc64 as crc64_mod
from nadare.core import directdl as dd

DATA = b"Arknights.exe bytes" * 50
GOOD = crc64_mod.crc64(DATA)


def _file(url: str, crc: int | None = GOOD, size: int | None = len(DATA)) -> dd.RemoteFile:
    return dd.RemoteFile("Arknights.exe", url, size, crc64=crc)


def test_a_matching_crc64_verifies(tmp_path):
    path = tmp_path / "Arknights.exe"
    path.write_bytes(DATA)
    assert dd._verify_remote(path, _file("http://unused"))


def test_a_wrong_crc64_does_not_verify(tmp_path):
    path = tmp_path / "Arknights.exe"
    path.write_bytes(DATA)
    assert not dd._verify_remote(path, _file("http://unused", crc=GOOD ^ 1))


def test_same_size_other_bytes_do_not_verify(tmp_path):
    path = tmp_path / "Arknights.exe"
    path.write_bytes(DATA[:-1] + b"!")
    assert not dd._verify_remote(path, _file("http://unused"))


def test_a_crc64_of_zero_is_still_checked(tmp_path):
    """0 is a real CRC value (the empty file's); only ``None`` means "not declared"."""
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert dd._verify_remote(path, dd.RemoteFile("empty.bin", "http://unused", 0, crc64=0))
    path.write_bytes(b"x")
    assert not dd._verify_remote(path, dd.RemoteFile("empty.bin", "http://unused", 1, crc64=0))


def test_sync_downloads_and_checks_the_crc64(server, tmp_path):  # noqa: F811
    srv = server({"/Arknights.exe": DATA})
    dd.sync(dd.Plan("Arknights_EN", "1", files=(_file(srv.url + "/Arknights.exe"),)), tmp_path)
    assert (tmp_path / "Arknights.exe").read_bytes() == DATA


def test_a_download_with_the_wrong_crc64_is_refused_and_the_old_file_kept(server, tmp_path):  # noqa: F811
    old = b"the installed build"
    (tmp_path / "Arknights.exe").write_bytes(old)
    bad = DATA[:-1] + b"!"
    srv = server({"/Arknights.exe": bad})
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("Arknights_EN", "2", files=(_file(srv.url + "/Arknights.exe"),)), tmp_path)
    assert (tmp_path / "Arknights.exe").read_bytes() == old


def test_a_file_already_right_is_not_downloaded_again(server, tmp_path):  # noqa: F811
    (tmp_path / "Arknights.exe").write_bytes(DATA)
    srv = server({})  # any request would 404
    summary = dd.sync(
        dd.Plan("Arknights_EN", "1", files=(_file(srv.url + "/Arknights.exe"),)), tmp_path
    )
    assert summary["downloaded"] == 0


def test_the_downloader_keeps_a_present_file_that_matches(server, tmp_path):  # noqa: F811
    """``_download_remote`` itself (also used for kept archives) checks the CRC-64 first."""
    path = tmp_path / "Arknights.exe"
    path.write_bytes(DATA)
    srv = server({})  # a download would 404
    dd._download_remote(_file(srv.url + "/Arknights.exe"), path, emit=None, cancel=None)
    assert path.read_bytes() == DATA
