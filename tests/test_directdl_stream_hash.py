# SPDX-License-Identifier: GPL-3.0-or-later
"""A streamed install must never claim an archive hash it did not verify."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl as dd

SEVENZ = Path(__file__).parent / "fixtures" / "sevenz"


@pytest.fixture
def server():
    made: list[Server] = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)
    return buf.getvalue()


def content() -> dict[str, bytes]:
    return {"data/a.bin": b"A" * 4000, "data/b.bin": b"B" * 5000, "readme.txt": b"hello\n"}


def zip_plan(url: str, blob: bytes, *, sha256: str | None = None, md5: str | None = None):
    src = dd.RemoteFile("g.zip", url, len(blob), sha256=sha256, md5=md5)
    return dd.Plan("T", "1", archives=(dd.Archive("g", src, unwrap=("zip",), stream=True),))


def sevenz_plan(url: str, blob: bytes, *, sha256: str | None = None):
    src = dd.RemoteFile("g.7z", url, len(blob), sha256=sha256)
    return dd.Plan("T", "1", archives=(dd.Archive("g", src, unwrap=("7z",), stream=True),))


def done_record(dest: Path) -> dict:
    path = dest / dd.STATE_DIR / "g.done"
    return json.loads(path.read_text(encoding="utf-8"))


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (dd.STATE_DIR, dd.DL_DIR, "xdg")
    }


def case(kind: str) -> tuple[str, bytes]:
    """The URL path and archive bytes for a streamed zip or 7z install."""
    if kind == "zip":
        return "/g.zip", make_zip(content())
    return "/g.7z", (SEVENZ / "lzma2.7z").read_bytes()


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_a_wrong_index_sha256_fails_and_writes_no_record(server, tmp_path, kind):
    url, blob = case(kind)
    s = server({url: blob})
    plan = (zip_plan if kind == "zip" else sevenz_plan)(
        s.url + url, blob, sha256=hashlib.sha256(b"not the archive").hexdigest()
    )
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path)
    assert tree(tmp_path) == {}  # nothing was installed
    assert not (tmp_path / dd.STATE_DIR / "g.done").exists()
    assert not s.ranges  # a whole-archive sha256 is checked by the normal download path


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_a_right_index_sha256_installs_and_records_it(server, tmp_path, kind):
    url, blob = case(kind)
    sha256 = hashlib.sha256(blob).hexdigest()
    s = server({url: blob})
    plan = (zip_plan if kind == "zip" else sevenz_plan)(s.url + url, blob, sha256=sha256)
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert not s.ranges  # a whole-archive sha256 is checked by the normal download path
    record = done_record(tmp_path)
    assert record["sha256"] == sha256  # the record only carries a hash this run verified
    assert "declared_sha256" not in record
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    if kind == "zip":
        assert tree(tmp_path) == content()
    else:
        expected = tree(SEVENZ / "expected")
        assert {k: v for k, v in tree(tmp_path).items() if k in expected} == expected


def test_a_streamed_install_records_the_declared_md5_as_unverified(server, tmp_path):
    blob = make_zip(content())
    md5 = hashlib.md5(blob).hexdigest()
    s = server({"/g.zip": blob})
    plan = zip_plan(s.url + "/g.zip", blob, md5=md5)
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert s.ranges  # it really streamed
    assert tree(tmp_path) == content()
    record = done_record(tmp_path)
    assert "md5" not in record and "sha256" not in record
    assert record["declared_md5"] == md5
    assert record["verified"] == "members"
    assert record["member_crc32"]
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
