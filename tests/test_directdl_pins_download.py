# SPDX-License-Identifier: GPL-3.0-or-later
"""A pin also checks an archive that is downloaded whole, not streamed (#788, BD2).

BD2's outer archive has no published size or hash, only the inner zip's CRC-32, which a
crafted file can match. An index pin of the outer file closes that.
"""

from __future__ import annotations

import dataclasses
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _httpserver import Server

from nadare.backends import direct
from nadare.core import archivepins
from nadare.core import directdl as dd

BS = 4096


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


def stored_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)
    return buf.getvalue()


GOOD = {"data/a.bin": b"A" * 9000, "readme.txt": b"hello\n"}
EVIL = {"data/a.bin": b"A" * 4000 + b"EVIL" + b"A" * 4996, "readme.txt": b"hello\n"}


def plan(url: str, pinned: bytes | None, *, size: int | None = None) -> dd.Plan:
    """A BD2-shaped plan: one archive, no size or hash, downloaded whole."""
    source = dd.RemoteFile("g.dat", url, size)
    archive = dd.Archive("g", source, unwrap=("zip",))
    if pinned is not None:
        archive = dataclasses.replace(archive, pin=archivepins.pin_from_bytes(url, pinned, BS))
    return dd.Plan("T", "1", archives=(archive,))


def installed(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (dd.STATE_DIR, dd.DL_DIR, "xdg")
    }


def record(dest: Path) -> dict:
    return json.loads((dest / dd.STATE_DIR / "g.done").read_text(encoding="utf-8"))


def test_a_pinned_download_installs_and_records_the_pin(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.dat": blob})
    p = plan(s.url + "/g.dat", blob)
    assert dd.sync(p, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert installed(tmp_path) == GOOD
    assert record(tmp_path)["pin_fingerprint"] == p.archives[0].pin.fingerprint()
    assert dd.sync(p, tmp_path) == {"downloaded": 0, "skipped": 1}


def test_a_swapped_download_with_valid_crcs_is_refused_and_dropped(server, tmp_path):
    good, evil = stored_zip(GOOD), stored_zip(EVIL)
    assert len(good) == len(evil)
    s = server({"/g.dat": evil})
    with pytest.raises(dd.DirectError) as caught:
        dd.sync(plan(s.url + "/g.dat", good), tmp_path)
    assert "pin" in str(caught.value) or "checksum list" in str(caught.value)
    assert installed(tmp_path) == {}
    assert not (tmp_path / dd.STATE_DIR / "g.done").exists()
    assert not [p for p in (tmp_path / dd.DL_DIR).rglob("*") if p.is_file()]


def test_a_download_of_the_wrong_size_is_refused(server, tmp_path):
    good = stored_zip(GOOD)
    s = server({"/g.dat": good + b"\0" * 10})
    with pytest.raises(dd.DirectError):
        dd.sync(plan(s.url + "/g.dat", good), tmp_path)
    assert not (tmp_path / dd.STATE_DIR / "g.done").exists()


def test_without_a_pin_a_whole_download_is_unchanged(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.dat": blob})
    dd.sync(plan(s.url + "/g.dat", None), tmp_path)
    assert installed(tmp_path) == GOOD
    assert "pin_fingerprint" not in record(tmp_path)


def test_a_new_pin_does_not_download_an_existing_install_again(server, tmp_path):
    """BD2 players installed before the pin existed: no surprise 400 MB re-download."""
    blob = stored_zip(GOOD)
    s = server({"/g.dat": blob})
    url = s.url + "/g.dat"
    dd.sync(plan(url, None), tmp_path)
    assert dd.sync(plan(url, blob), tmp_path) == {"downloaded": 0, "skipped": 1}


def test_the_bd2_backend_attaches_a_pin_to_its_downloaded_archive(tmp_path, monkeypatch):
    url = "https://pc.bd2.pmang.cloud/browndust2starter/games/10000001/1.tied.dat"
    data = b"z" * 10_000
    source = dd.RemoteFile("1.tied.dat", url)
    bd2_plan = dd.Plan(
        "bd2", "1", archives=(dd.Archive("1.tied.dat", source, unwrap=("zip", "zip")),)
    )
    monkeypatch.setattr(
        direct.BrownDust2Backend, "plan_factory", staticmethod(lambda fetch=None: bd2_plan)
    )
    pinned = archivepins.pin_from_bytes(url, data, BS)
    monkeypatch.setattr(
        archivepins,
        "load_state",
        lambda tag: archivepins.Loaded(
            {"1.tied.dat": pinned} if tag == "bd2" else {}, available=True, known=True
        ),
    )
    backend = direct.BrownDust2Backend(make_context(tmp_path, "direct-bd2", {}))
    assert backend.plan().archives[0].pin == pinned
