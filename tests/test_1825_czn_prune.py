# SPDX-License-Identifier: GPL-3.0-or-later
"""#1825: a CZN update removes the files the new manifest no longer lists.

The updater syncs only the files that differ, so it records the whole manifest's files itself
(``directdl.prune_dropped_files``). The first update after this change has no ledger yet: the
cached manifest of the version installed before is the list. Prerequisite folders (vcredist,
DirectX ...) are never managed by the updater and never removed.
"""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from test_directdl_prune import server  # noqa: F401 - fixture reuse

from nadare.backends.czn import update as czn_update
from nadare.core import directdl


def _row(path: str, data: bytes, chunk: str) -> str:
    packed = gzip.compress(data, mtime=0)
    md5u, md5p = hashlib.md5(data).hexdigest(), hashlib.md5(packed).hexdigest()
    return f"|F|{path}|3|{chunk}|x|{len(data)}|x|{md5u}|{md5p}"


V1 = {"/bin/game.exe": b"MZ v1", "/bin/old.dll": b"old dll" * 9, "/data/keep.pak": b"k" * 40}
V2 = {"/bin/game.exe": b"MZ v2!", "/data/keep.pak": b"k" * 40, "/data/new.pak": b"n" * 30}


def _manifest(version: int, contents: dict[str, bytes]) -> dict:
    rows = [_row(p, d, f"c{version}-{i}") for i, (p, d) in enumerate(contents.items())]
    return {"version_no": version, "files": rows}


@pytest.fixture
def czn(server, tmp_path, monkeypatch):  # noqa: F811
    objects = {}
    for version, contents in ((1, V1), (2, V2)):
        for i, data in enumerate(contents.values()):
            objects[f"/v3/c{version}-{i}.gz"] = gzip.compress(data, mtime=0)
    srv = server(objects)
    game = tmp_path / "game"
    monkeypatch.setattr(czn_update, "GAME_DIR", game)
    monkeypatch.setattr(czn_update, "BASE", srv.url)
    monkeypatch.setattr(czn_update, "MANIFEST_CACHE", tmp_path / "state" / "manifest.json")
    monkeypatch.setattr(czn_update, "STAT_CACHE", tmp_path / "state" / "stat.json", raising=False)
    monkeypatch.setattr(czn_update, "UPF", tmp_path / "state" / "game.upf")
    monkeypatch.setattr(czn_update, "game_running", lambda: False)
    monkeypatch.setattr(czn_update, "diskspace", None)
    return game


def _update(manifest: dict) -> int:
    to_dl, dirs = czn_update.plan(manifest, trust_stat_cache=False)
    if not to_dl:
        return czn_update._apply_unchanged(manifest, int(manifest["version_no"]))
    sync_plan = czn_update.to_plan(to_dl, manifest["version_no"])
    total = sum(int(r[5] or 0) for r in to_dl)
    return czn_update._apply(manifest, manifest["version_no"], sync_plan, to_dl, dirs, total)


def test_an_update_removes_the_file_the_manifest_dropped(czn):
    assert _update(_manifest(1, V1)) == 0
    assert (czn / "bin/old.dll").is_file()
    assert _update(_manifest(2, V2)) == 0
    assert not (czn / "bin/old.dll").exists()
    assert (czn / "data/new.pak").is_file()
    assert (czn / "data/keep.pak").read_bytes() == V2["/data/keep.pak"]


def test_an_install_from_before_the_ledger_uses_the_cached_manifest(czn):
    assert _update(_manifest(1, V1)) == 0
    (czn / directdl.STATE_DIR / f"files-{czn_update.SERVICE}.json").unlink()
    assert _update(_manifest(2, V2)) == 0
    assert not (czn / "bin/old.dll").exists()


def test_a_changed_file_is_kept(czn):
    assert _update(_manifest(1, V1)) == 0
    (czn / "bin/old.dll").write_bytes(b"OLD DLL" * 9)
    assert _update(_manifest(2, V2)) == 0
    assert (czn / "bin/old.dll").read_bytes() == b"OLD DLL" * 9


def test_prerequisites_are_never_removed(czn):
    v1 = {**V1, "/vcredist/vc_redist.x64.exe": b"redist"}
    (czn / "vcredist").mkdir(parents=True)
    (czn / "vcredist/vc_redist.x64.exe").write_bytes(b"redist")
    assert _update(_manifest(1, v1)) == 0
    assert _update(_manifest(2, V2)) == 0
    assert (czn / "vcredist/vc_redist.x64.exe").is_file()


def test_a_release_that_only_drops_files_still_removes_them(czn):
    assert _update(_manifest(1, V1)) == 0
    smaller = {p: d for p, d in V1.items() if p != "/bin/old.dll"}
    assert _update(_manifest(2, smaller)) == 0
    assert not (czn / "bin/old.dll").exists()


def test_a_failed_update_removes_nothing(czn, monkeypatch):
    assert _update(_manifest(1, V1)) == 0
    monkeypatch.setattr(czn_update, "BASE", "http://127.0.0.1:9")
    assert _update(_manifest(2, V2)) != 0
    assert (czn / "bin/old.dll").is_file()
    ledger = json.loads((czn / directdl.STATE_DIR / f"files-{czn_update.SERVICE}.json").read_text())
    assert ledger["version"] == "1"


def test_nothing_is_removed_while_the_game_runs_and_it_is_done_later(czn, monkeypatch):
    assert _update(_manifest(1, V1)) == 0
    smaller = {p: d for p, d in V1.items() if p != "/bin/old.dll"}
    monkeypatch.setattr(czn_update, "game_running", lambda: True)
    assert _update(_manifest(2, smaller)) == 0  # the marker still advances
    assert (czn / "bin/old.dll").is_file()
    monkeypatch.setattr(czn_update, "game_running", lambda: False)
    assert _update(_manifest(2, smaller)) == 0
    assert not (czn / "bin/old.dll").exists()
