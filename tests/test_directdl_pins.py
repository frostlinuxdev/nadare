# SPDX-License-Identifier: GPL-3.0-or-later
"""Streamed installs verify every block they read when the archive has a pin."""

from __future__ import annotations

import dataclasses
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import archivepins
from nadare.core import directdl as dd

BS = 4096
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


def stored_zip(files: dict[str, bytes]) -> bytes:
    """Uncompressed members, so two archives with different content have the same size."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)
    return buf.getvalue()


GOOD = {"data/a.bin": b"A" * 9000, "data/b.bin": b"B" * 9000, "readme.txt": b"hello\n"}
EVIL = {**GOOD, "data/b.bin": b"B" * 4000 + b"EVIL" + b"B" * 4996}  # same size, valid CRCs


def plan_with_pin(url: str, served: bytes, pinned: bytes, unwrap=("zip",)):
    source = dd.RemoteFile("g.bin", url, len(served))
    archive = dd.Archive("g", source, unwrap=unwrap, stream=True)
    pin = archivepins.pin_from_bytes(url, pinned, BS)
    return dd.Plan("T", "1", archives=(dataclasses.replace(archive, pin=pin),))


def installed(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (dd.STATE_DIR, dd.DL_DIR, "xdg")
    }


def record(dest: Path) -> dict:
    return json.loads((dest / dd.STATE_DIR / "g.done").read_text(encoding="utf-8"))


def test_a_pinned_zip_installs_and_the_record_says_it_was_pin_verified(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.zip": blob})
    plan = plan_with_pin(s.url + "/g.zip", blob, blob)
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert installed(tmp_path) == GOOD
    assert record(tmp_path)["verified"] == "pin"
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}


def test_requests_are_aligned_to_the_pins_block_size(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.zip": blob})
    dd.sync(plan_with_pin(s.url + "/g.zip", blob, blob), tmp_path)
    assert s.ranges and all(start % BS == 0 for _path, start, _end in s.ranges), s.ranges


def test_a_swapped_archive_with_valid_crcs_is_refused_and_nothing_is_recorded(server, tmp_path):
    good, evil = stored_zip(GOOD), stored_zip(EVIL)
    assert len(good) == len(evil)
    s = server({"/g.zip": evil})
    plan = plan_with_pin(s.url + "/g.zip", evil, good)  # the pin was made from the real archive
    with pytest.raises(dd.DirectError) as caught:
        dd.sync(plan, tmp_path)
    message = str(caught.value)
    assert "checksum list" in message and "g.zip" in message
    assert b"EVIL" not in b"".join(installed(tmp_path).values())
    assert not (tmp_path / dd.STATE_DIR / "g.done").exists()


def test_without_a_pin_the_install_is_unchanged(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.zip": blob})
    source = dd.RemoteFile("g.bin", s.url + "/g.zip", len(blob))
    plan = dd.Plan("T", "1", archives=(dd.Archive("g", source, unwrap=("zip",), stream=True),))
    dd.sync(plan, tmp_path)
    assert installed(tmp_path) == GOOD
    assert record(tmp_path)["verified"] == "members"


def test_a_7z_is_checked_too(server, tmp_path):
    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    plan = plan_with_pin(s.url + "/g.7z", blob, blob, unwrap=("7z",))
    dd.sync(plan, tmp_path)
    assert record(tmp_path)["verified"] == "pin"

    evil = blob[:40] + bytes([blob[40] ^ 0xFF]) + blob[41:]  # a flipped byte in the packed data
    s2 = server({"/g.7z": evil})
    bad_plan = plan_with_pin(s2.url + "/g.7z", evil, blob, unwrap=("7z",))
    other = tmp_path / "second"
    with pytest.raises(dd.DirectError) as caught:
        dd.sync(bad_plan, other)
    assert "checksum list" in str(caught.value)
    assert not (other / dd.STATE_DIR / "g.done").exists()


def test_a_server_that_refuses_ranges_cannot_bypass_the_pin(server, tmp_path):
    """Answering 200 to a Range request must not make us fall back to an unchecked download."""
    good, evil = stored_zip(GOOD), stored_zip(EVIL)
    s = server({"/g.zip": evil})
    s.ignore_range = True
    plan = plan_with_pin(s.url + "/g.zip", evil, good)
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path)
    assert b"EVIL" not in b"".join(installed(tmp_path).values())
    assert not (tmp_path / dd.STATE_DIR / "g.done").exists()


def test_a_plan_without_an_archive_size_still_installs_through_its_pin(server, tmp_path):
    blob = stored_zip(GOOD)
    s = server({"/g.zip": blob})
    source = dd.RemoteFile("g.bin", s.url + "/g.zip", None)  # like the Aether Gazer source
    pin = archivepins.pin_from_bytes(s.url + "/g.zip", blob, BS)
    bare = dd.Plan(
        "T", "1", archives=(dd.Archive("g", source, unwrap=("zip",), stream=True),)
    )
    plan = archivepins.attach(bare, {"g": pin})
    assert plan.archives[0].pin == pin
    dd.sync(plan, tmp_path)
    assert installed(tmp_path) == GOOD
    assert record(tmp_path)["verified"] == "pin"


def test_a_corrected_pin_makes_an_installed_archive_verify_again(server, tmp_path):
    """The record is tied to the pin it was verified with, so a new pin is not ignored."""
    good, evil = stored_zip(GOOD), stored_zip(EVIL)
    s = server({"/g.zip": good})
    dd.sync(plan_with_pin(s.url + "/g.zip", good, good), tmp_path)
    assert installed(tmp_path)["data/b.bin"] == GOOD["data/b.bin"]

    s.objects["/g.zip"] = evil  # the publisher really did republish, same size
    result = dd.sync(plan_with_pin(s.url + "/g.zip", evil, evil), tmp_path)
    assert result == {"downloaded": 1, "skipped": 0}
    assert installed(tmp_path)["data/b.bin"] == EVIL["data/b.bin"]
    assert "pin_fingerprint" in record(tmp_path)
