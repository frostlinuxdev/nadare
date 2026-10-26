# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the cheap wins found by the optimisation audit."""

from __future__ import annotations

import time
import types
from pathlib import Path

from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import wine
from nadare.core import sevenz


def _fake_proc(root: Path, pid: int, argv: list[str], env: dict[str, str]) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def test_args_only_query_never_reads_environ(tmp_path, monkeypatch):
    _fake_proc(tmp_path, 4242, ["steam", "steam://rungameid/1"], {"HOME": "/h"})
    reads = []
    real = wine._environ_map
    monkeypatch.setattr(wine, "_environ_map", lambda entry: reads.append(entry) or real(entry))
    query = wine.ProcessQuery(args=("steam://rungameid/1",))
    assert wine._scan_query(query, proc_root=tmp_path) == [4242]
    assert reads == []  # nothing in the query can depend on the environment


def test_prefix_query_still_reads_environ(tmp_path):
    _fake_proc(tmp_path, 4243, ["wine", "game.exe"], {"WINEPREFIX": "/pfx"})
    _fake_proc(tmp_path, 4244, ["wine", "game.exe"], {"WINEPREFIX": "/other"})
    assert wine._scan_query(wine.ProcessQuery(prefix="/pfx"), proc_root=tmp_path) == [4243]


def test_a_pack_extent_over_many_folders_is_linear():
    n = 30000
    streams = types.SimpleNamespace(pack_sizes=[7] * n, pack_pos=0)
    folders = [types.SimpleNamespace(packed_start=i, packed_indices=[0]) for i in range(n)]
    start = time.perf_counter()
    extents = [sevenz._pack_extent(streams, f) for f in folders]
    assert time.perf_counter() - start < 5.0  # generous guard against quadratic regressions
    assert extents[0] == (32, 7) and extents[1234] == (32 + 7 * 1234, 7)


def test_status_resolves_the_launcher_once(tmp_path, monkeypatch):
    backend = ak.ArknightsBackend(make_context(tmp_path, ak.KIND, None, "t"))
    calls = []
    real = backend.find_launcher
    monkeypatch.setattr(backend, "find_launcher", lambda: calls.append(1) or real())
    monkeypatch.setattr(ak, "scan_pids", lambda patterns: [])
    backend.status()  # no launcher installed: the answer "none" must not be searched for again
    assert len(calls) == 1


def test_hero_card_scales_the_art_once_per_size(qapp):
    from PyQt6.QtGui import QColor, QPixmap

    from nadare.gui.game_view import HeroCard

    source = QPixmap(1280, 300)
    source.fill(QColor("navy"))
    card = HeroCard()
    card.resize(640, 200)
    card.set_source(source)
    card.grab()
    first = card._cropped
    assert first is not None
    card.grab()
    assert card._cropped is first  # a repaint at the same size reuses the crop
    card.resize(500, 200)
    card.grab()
    assert card._cropped is not first


def test_plan_missing_without_verify_checks_sizes_only(tmp_path, monkeypatch):
    import hashlib

    from nadare.core import directdl as dd

    data = b"d" * 64
    (tmp_path / "ok.bin").write_bytes(data)
    (tmp_path / "short.bin").write_bytes(data[:10])
    (tmp_path / "flipped.bin").write_bytes(b"e" * 64)  # right size, wrong content
    md5 = hashlib.md5(data).hexdigest()
    plan = dd.Plan(
        "T",
        "1",
        files=tuple(
            dd.RemoteFile(n, "https://x/" + n, 64, md5=md5)
            for n in ("ok.bin", "short.bin", "flipped.bin", "gone.bin")
        ),
    )
    monkeypatch.setattr(
        dd, "hash_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("hashed"))
    )
    files, _ = dd.plan_missing(plan, tmp_path, verify=False)
    assert sorted(f.path for f in files) == ["gone.bin", "short.bin"]


def test_yostar_plan_without_verify_does_not_crc(tmp_path, monkeypatch):
    from nadare.core import yostar

    (tmp_path / "a.bin").write_bytes(b"x" * 8)
    entries = [
        yostar.ManifestFile(path="a.bin", size=8, crc64=1),
        yostar.ManifestFile(path="b.bin", size=8, crc64=1),
    ]
    manifest = types.SimpleNamespace(files=entries)
    monkeypatch.setattr(
        yostar, "crc64_file", lambda p: (_ for _ in ()).throw(AssertionError("crc"))
    )
    assert [e.path for e in yostar.plan(manifest, tmp_path, verify=False)] == ["b.bin"]


def test_repair_checks_files_after_a_sync_without_hashing_again(tmp_path, monkeypatch):
    backend = ak.ArknightsBackend(make_context(tmp_path, ak.KIND, None, "t"))
    seen = []
    monkeypatch.setattr(backend, "direct_mode", lambda: True)
    monkeypatch.setattr(backend, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(backend, "_sync_direct", lambda: {})
    monkeypatch.setattr(backend, "_missing_entries", lambda **kw: seen.append(kw) or [])
    backend.repair()
    assert seen == [{"verify": False}]
