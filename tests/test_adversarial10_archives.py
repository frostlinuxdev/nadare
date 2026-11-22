# SPDX-License-Identifier: GPL-3.0-or-later
"""Malformed archive paths and corrupt archives must leave installed bytes alone."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest

from nadare.core import directdl as dd


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "a/../../escape",
        "/escape",
        r"..\escape",
        ".nadare-downloads/record",
        ".",
        "a/..",
    ],
)
def test_archive_rejects_escaping_or_reserved_members(tmp_path, name):
    archive, dest, cache = tmp_path / "bad.zip", tmp_path / "game", tmp_path / "cache"
    dest.mkdir()
    cache.mkdir()
    (dest / "save.dat").write_bytes(b"keep")
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("valid.bin", b"valid")
        z.writestr(name, b"evil")
    with pytest.raises(dd.DirectError):
        dd._extract_zip_into_dest(archive, dest, cache)
    assert sorted(p.name for p in dest.iterdir()) == ["save.dat"]
    assert (dest / "save.dat").read_bytes() == b"keep"
    assert not list(cache.iterdir())


@pytest.mark.parametrize("cut", [1, 10, 40, 80])
def test_truncated_archive_never_changes_existing_install(tmp_path, cut):
    archive, dest, cache = tmp_path / "bad.zip", tmp_path / "game", tmp_path / "cache"
    dest.mkdir()
    cache.mkdir()
    (dest / "game.bin").write_bytes(b"installed")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("game.bin", b"replacement" * 100)
    archive.write_bytes(data.getvalue()[:-cut])
    with pytest.raises(dd.DirectError):
        dd._extract_zip_into_dest(archive, dest, cache)
    assert (dest / "game.bin").read_bytes() == b"installed"
    assert not list(cache.iterdir())


def test_archive_symlink_is_refused_before_install_changes(tmp_path):
    archive, dest, cache = tmp_path / "bad.zip", tmp_path / "game", tmp_path / "cache"
    dest.mkdir()
    cache.mkdir()
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("game.bin", b"valid")
        z.writestr(link, "../outside")
    with pytest.raises(dd.DirectError):
        dd._extract_zip_into_dest(archive, dest, cache)
    assert not list(dest.iterdir())


@pytest.mark.parametrize(
    "name", ["space name/data.bin", "ゲーム/data.bin", "a/b/c.bin", "C:/literal-linux-folder.bin"]
)
def test_valid_archive_preserves_unrelated_files(tmp_path, name):
    archive, dest, cache = tmp_path / "good.zip", tmp_path / "game", tmp_path / "cache"
    dest.mkdir()
    cache.mkdir()
    (dest / "save.dat").write_bytes(b"keep")
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, b"game")
    dd._extract_zip_into_dest(archive, dest, cache)
    assert (dest / name).read_bytes() == b"game"
    assert (dest / "save.dat").read_bytes() == b"keep"
