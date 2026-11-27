# SPDX-License-Identifier: GPL-3.0-or-later
"""#1833: the pieces of a stopped range download do not stay in the download cache forever.

A file over 256 MiB downloads in ranges: ``<x>.part-N``, ``<x>.part-N.done`` and
``<x>.part.ranges.json``. Only the same target ever cleared them, so a next version whose
archive has another name (Endfield, DNA ...) left gigabytes in ``.nadare-dl``. The stale-part
sweep after a successful sync treats them as their ``.part``: ours in the download cache, for a
managed file, or for a Nadare-only staging name; anything else (a game's own file) stays.
"""

from __future__ import annotations

from nadare.core import directdl as dd

PIECES = ("x.7z.part-0", "x.7z.part-1.done", "x.7z.part-12", "x.7z.part.ranges.json")


def _write(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"piece")


def test_range_pieces_in_the_download_cache_are_removed(tmp_path):
    cache = tmp_path / dd.DL_DIR
    _write(cache, PIECES)
    dd._remove_stale_parts(tmp_path, keep=set(), managed=set())
    assert not any((cache / name).exists() for name in PIECES)


def test_range_pieces_of_a_managed_file_are_removed(tmp_path):
    root = tmp_path / "game"
    _write(root, ("game.bin.part-0", "game.bin.part-1.done", "game.bin.part.ranges.json"))
    dd._remove_stale_parts(root, keep=set(), managed={"game.bin"})
    assert list(root.iterdir()) == []


def test_look_alike_files_of_the_game_stay(tmp_path):
    names = ("movie.part-0", "level.part-2.done", "save.part.ranges.json", "x.part-a")
    _write(tmp_path / "Data", names)
    dd._remove_stale_parts(tmp_path, keep=set(), managed={"Data/other.bin"})
    assert all((tmp_path / "Data" / name).exists() for name in names)


def test_a_successful_sync_clears_a_renamed_archives_pieces(tmp_path):
    cache = tmp_path / dd.DL_DIR
    _write(cache, ("old-version.zip.part-0", "old-version.zip.part.ranges.json"))
    dd.sync(dd.Plan("T", "2"), tmp_path)
    assert not cache.exists()
