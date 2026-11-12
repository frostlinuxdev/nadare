# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: Kuro's .krpdiff patches, applied by Nadare itself (no hpatchz)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from _krpdiff import build, varint  # noqa: E402

from nadare.core import krdiff


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def write_tree(root: Path, files: list[tuple[str, bytes]]) -> None:
    for path, data in files:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@pytest.fixture()
def tree(tmp_path):
    old = [("Client/a.pak", bytes(range(256)) * 40), ("Client/b.pak", b"B" * 3000 + b"tail")]
    write_tree(tmp_path / "old", old)
    return tmp_path, old


# ---- the format ------------------------------------------------------------------------------


def test_the_varint_encoder_round_trips():
    import io

    for value in (0, 1, 63, 64, 127, 128, 300, 2**20, 2**40 + 7):
        assert krdiff._varint(io.BytesIO(varint(value))) == value
        for sign in (0, 1):
            raw = varint(value, 1, sign)
            first = raw[0]
            got = krdiff._varint(io.BytesIO(raw[1:]), 1, first)
            assert got == value and (first >> 7) == sign


@pytest.mark.parametrize("compress", [True, False])
def test_multi_file_patch_with_gaps_jumps_and_a_wrap(tree, compress):
    root, old = tree
    blob = b"".join(b for _, b in old)
    a = old[0][1]
    new_a = b"HEAD" + a[100:2000] + b"MID" + blob[10_000:10_500]  # crosses into b.pak
    new_b = a[50:60] + b"xyz"  # jumps backwards
    new_c = b"brand new file"  # no old bytes at all
    ops = [
        ("new", b"HEAD"), ("old", 100, 1900), ("new", b"MID"), ("old", 10_000, 500),
        ("old", 50, 10), ("new", b"xyz"), ("new", new_c),
    ]
    patch = build(old, [("Client/a.pak", new_a), ("Client/b.pak", new_b), ("Client/c.pak", new_c)],
                  ops, compress=compress, dirs=("Client/", "Client/New/"))
    (root / "p.krpdiff").write_bytes(patch)
    got = krdiff.apply(root / "p.krpdiff", root / "old", root / "out")
    assert got == {
        "Client/a.pak": md5(new_a), "Client/b.pak": md5(new_b), "Client/c.pak": md5(new_c)
    }
    assert (root / "out" / "Client" / "a.pak").read_bytes() == new_a
    assert (root / "out" / "Client" / "New").is_dir()
    assert (root / "old" / "Client" / "a.pak").read_bytes() == a  # old files untouched


def test_the_read_position_wraps_around_the_old_data(tree):
    root, old = tree
    a = old[0][1]
    new = a[200:300] + a[0:50]
    patch = build(old, [("Client/a.pak", new)], [("old", 200, 100), ("old", 0, 50)], wrap=True)
    (root / "p.krpdiff").write_bytes(patch)
    assert krdiff.apply(root / "p.krpdiff", root / "old", root / "out") == {
        "Client/a.pak": md5(new)
    }


def test_progress_reaches_the_total_and_cancel_stops(tree):
    from nadare.core.events import Cancel, Cancelled

    root, old = tree
    new = old[0][1] * 3
    size = len(old[0][1])
    patch = build(old, [("Client/a.pak", new)], [("old", 0, size)] * 3)
    (root / "p.krpdiff").write_bytes(patch)
    seen = []
    krdiff.apply(
        root / "p.krpdiff", root / "old", root / "out", progress=lambda d, t: seen.append((d, t))
    )
    assert seen[-1] == (len(new), len(new))
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        krdiff.apply(root / "p.krpdiff", root / "old", root / "out2", cancel=cancel)


# ---- refusals --------------------------------------------------------------------------------


def _apply(root: Path, patch: bytes, out: str = "out"):
    (root / "p.krpdiff").write_bytes(patch)
    return krdiff.apply(root / "p.krpdiff", root / "old", root / out)


@pytest.mark.parametrize("bad", ["../escape.pak", "/abs.pak", "a/../../x"])
def test_a_path_leaving_the_folder_is_refused(tree, bad):
    root, old = tree
    with pytest.raises(krdiff.KrDiffError, match="unsafe path"):
        _apply(root, build(old, [(bad, b"x")], [("new", b"x")]))
    assert not (root / "escape.pak").exists()


def test_reading_past_the_old_files_is_refused(tree):
    root, old = tree
    total = sum(len(b) for _, b in old)
    with pytest.raises(krdiff.KrDiffError):
        _apply(root, build(old, [("Client/a.pak", b"x" * 20)], [("old", total - 10, 20)]))


def test_an_old_file_of_the_wrong_size_is_refused(tree):
    root, old = tree
    (root / "old" / "Client" / "b.pak").write_bytes(b"shorter")
    with pytest.raises(krdiff.KrDiffError, match="not the file"):
        _apply(root, build(old, [("Client/a.pak", b"x")], [("new", b"x")]))


def test_a_linked_old_file_is_not_followed(tree):
    root, old = tree
    victim = root / "secret"
    victim.write_bytes(old[1][1])
    (root / "old" / "Client" / "b.pak").unlink()
    os.symlink(victim, root / "old" / "Client" / "b.pak")
    with pytest.raises(OSError):
        _apply(root, build(old, [("Client/a.pak", b"x")], [("new", b"x")]))


def test_an_existing_output_is_never_overwritten(tree):
    root, old = tree
    (root / "out" / "Client").mkdir(parents=True)
    (root / "out" / "Client" / "a.pak").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        _apply(root, build(old, [("Client/a.pak", b"x")], [("new", b"x")]))
    assert (root / "out" / "Client" / "a.pak").read_bytes() == b"keep"


def test_a_truncated_patch_is_refused(tree):
    root, old = tree
    patch = build(old, [("Client/a.pak", b"0123456789" * 10)], [("new", b"0123456789" * 10)],
                  compress=False)
    with pytest.raises(krdiff.KrDiffError):
        _apply(root, patch[:-5])


def test_a_section_claiming_more_than_the_limit_is_refused(tree, monkeypatch):
    root, old = tree
    monkeypatch.setattr(krdiff, "MAX_HEAD_BYTES", 10)
    with pytest.raises(krdiff.KrDiffError, match="larger than allowed"):
        _apply(root, build(old, [("Client/a.pak", b"x")], [("new", b"x")]))


def test_not_a_kuro_patch_is_refused(tree):
    root, _ = tree
    with pytest.raises(krdiff.KrDiffError, match="HDIFF19"):
        _apply(root, b"HDIFF13&zstd\0rest")


# ---- the real thing, when the fixtures are on this machine ------------------------------------

REAL = Path("/mnt/kikyou/work/b23run/krp")


@pytest.mark.skipif(not (REAL / "selected.json").is_file(), reason="real WuWa fixtures absent")
def test_real_wuwa_372_to_380_groups_come_out_md5_exact(tmp_path):
    cases = [(REAL / "small.krpdiff", REAL / "t1" / "old",
              {"Client/Content/HD/pakchunk28-HD-WindowsNoEditor.pak":
               "61cf52cb6807a1aedfbe5a7740f4f8f8"})]
    for item in json.loads((REAL / "selected.json").read_text()):
        group = item["group"]
        patch = REAL / group["dest"]
        sources = [REAL / "old372" / f["dest"] for f in group["srcFiles"]]
        if patch.is_file() and all(path.is_file() for path in sources):
            cases.append((patch, REAL / "old372", {f["dest"]: f["md5"] for f in group["dstFiles"]}))
    assert len(cases) >= 2
    for i, (patch, old_root, want) in enumerate(cases):
        assert krdiff.apply(patch, old_root, tmp_path / str(i)) == want


def test_a_cover_reading_past_the_old_files_writes_nothing_first(tree):
    root, old = tree
    total = sum(len(b) for _, b in old)
    with pytest.raises(krdiff.KrDiffError, match="outside the old files"):
        _apply(root, build(old, [("Client/a.pak", b"x" * 20)], [("old", total - 10, 20)]))
    assert (root / "out" / "Client" / "a.pak").stat().st_size == 0


def test_a_cover_writing_past_the_new_files_writes_nothing_first(tree):
    root, old = tree
    patch = build(old, [("Client/a.pak", b"0123456789")], [("old", 0, 30)])
    # The encoder sizes new files from the list (10 bytes); the cover copies 30.
    with pytest.raises(krdiff.KrDiffError, match="past its new files"):
        _apply(root, patch)
    assert (root / "out" / "Client" / "a.pak").stat().st_size == 0
