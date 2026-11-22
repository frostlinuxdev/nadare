# SPDX-License-Identifier: GPL-3.0-or-later
"""HDiffPatch single-stream directory diffs made from an empty folder (a full install).

Duet Night Abyss ships its whole client as one ``HDIFF19`` + ``HDIFFSF20`` (zstd) file made
against an empty folder. The fixtures were made with HDiffPatch's own ``hdiffz -SD -c-zstd
-C-fadler64`` (v4.x): ``full_small`` from an empty folder, ``delta_small`` from a non-empty one.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from nadare.core import hdiffsf, krdiff
from nadare.core.events import Cancel, Cancelled

FIX = Path(__file__).parent / "fixtures" / "hdiff"


def _expected() -> dict[str, str]:
    out = {}
    for line in (FIX / "full_small.md5").read_text().splitlines():
        md5, path = line.split(maxsplit=1)
        out[path.removeprefix("./")] = md5
    return out


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.md5(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_full_install_rebuilds_every_file(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    written = hdiffsf.extract(FIX / "full_small.hdiff", out)
    assert _tree(out) == _expected()
    assert sorted(written) == sorted(_expected())
    assert (out / "emptydir").is_dir()  # folders the diff lists are made too


def test_header_reports_files_and_sizes():
    with open(FIX / "full_small.hdiff", "rb") as f:
        head = hdiffsf.read_header(f)
    assert {e.path for e in head.files} == set(_expected())
    assert head.new_size == sum(e.size for e in head.files) == 70012
    assert head.compression == "zstd"


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"  # tmp_path itself holds the test's fake home (conftest)
    out.mkdir()
    return out


def test_progress_is_reported_up_to_the_total(tmp_path):
    seen = []
    hdiffsf.extract(
        FIX / "full_small.hdiff", _out(tmp_path), progress=lambda d, t: seen.append((d, t))
    )
    assert seen and seen[-1] == (70012, 70012)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_a_diff_against_old_files_is_refused(tmp_path):
    """Only a full install is supported: a diff that needs old files is not applied."""
    out = _out(tmp_path)
    with pytest.raises(hdiffsf.HDiffError, match="old files"):
        hdiffsf.extract(FIX / "delta_small.hdiff", out)
    assert not any(out.iterdir())


def test_a_truncated_file_is_refused(tmp_path):
    data = (FIX / "full_small.hdiff").read_bytes()
    bad = tmp_path / "bad.hdiff"
    bad.write_bytes(data[: len(data) - 500])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(hdiffsf.HDiffError):
        hdiffsf.extract(bad, out)


def test_trailing_garbage_is_refused(tmp_path):
    bad = tmp_path / "bad.hdiff"
    bad.write_bytes((FIX / "full_small.hdiff").read_bytes() + b"extra")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(hdiffsf.HDiffError):
        hdiffsf.extract(bad, out)


def test_a_damaged_stream_frame_is_refused(tmp_path):
    """A broken zstd frame is caught here. A flipped byte inside the compressed payload may
    not be (these frames carry no checksum): the caller checks the whole diff's MD5 first."""
    data = bytearray((FIX / "full_small.hdiff").read_bytes())
    with open(FIX / "full_small.hdiff", "rb") as f:
        offset = hdiffsf.read_header(f).data_offset
    data[offset : offset + 4] = b"\x00\x00\x00\x00"  # the zstd magic
    bad = tmp_path / "bad.hdiff"
    bad.write_bytes(bytes(data))
    with pytest.raises(hdiffsf.HDiffError):
        hdiffsf.extract(bad, _out(tmp_path))


def test_not_an_hdiff_is_refused(tmp_path):
    bad = tmp_path / "bad.hdiff"
    bad.write_bytes(b"PK\x03\x04 not a patch")
    with pytest.raises(hdiffsf.HDiffError):
        hdiffsf.extract(bad, _out(tmp_path))


def test_output_folder_must_be_empty(tmp_path):
    out = _out(tmp_path)
    (out / "EM.exe").write_bytes(b"user file")
    with pytest.raises(hdiffsf.HDiffError, match="empty"):
        hdiffsf.extract(FIX / "full_small.hdiff", out)
    assert (out / "EM.exe").read_bytes() == b"user file"


def test_a_symlink_in_the_way_is_not_followed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (out / "EM").symlink_to(outside)
    with pytest.raises((hdiffsf.HDiffError, OSError)):
        hdiffsf.extract(FIX / "full_small.hdiff", out)
    assert not any(outside.iterdir())


def test_cancel_stops_the_extraction(tmp_path):
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        hdiffsf.extract(FIX / "full_small.hdiff", _out(tmp_path), cancel=cancel)


def test_the_directory_head_parser_is_shared_with_krdiff():
    """One parser for HDiffPatch's HDIFF19 head (no second copy to drift, #1455)."""
    assert hdiffsf.read_dir_head is krdiff.read_dir_head
    with open(FIX / "full_small.hdiff", "rb") as f:
        head = krdiff.read_dir_head(f)
    assert head.old_size == 0
    assert {e.path for e in head.new_files} == set(_expected())


def test_bytes_input_is_not_accepted():
    with pytest.raises(TypeError):
        hdiffsf.extract(io.BytesIO(b""), Path("/nonexistent"))  # type: ignore[arg-type]
