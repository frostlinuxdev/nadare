# SPDX-License-Identifier: GPL-3.0-or-later
"""#1837: an update never deletes a dropped file the player changed.

Kuro (``deleteFiles`` and older patch lists), BPSR and Yostar remove what the publisher no
longer ships by name. Each finished sync now records what it installed (path, size and the
plan's md5 or CRC-64); a dropped file goes only while it still matches that record. A changed
one stays, is said once, and is not a leftover the page asks the player to repair. An install
with no record yet (an older Nadare) keeps the old behaviour: the name decides.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_bpsr_source as bpsr
import test_kuro_cleanup_reliable as kuro
import test_yostar_dropped_files_1452 as yo

from nadare.core import directdl as dd

# Fixtures of the modules above (assigned, not imported, so pytest finds them here).
cdn = yo.cdn
_offline_machinery = yo._offline_machinery
transport = bpsr.transport
wuwa = kuro.wuwa

INSTALLED = Path(dd.STATE_DIR) / "installed-files.json"


# ------------------------------------------------------------------ Yostar (Arknights)
def test_yostar_keeps_a_dropped_file_the_player_changed(cdn, tmp_path):
    backend = yo._install(cdn, tmp_path, yo.OLD)
    root = backend.install_dir()
    lines: list[str] = []
    backend.info = lambda text: lines.append(str(text))
    (root / "data/old.pak").write_bytes(b"player's own edit")
    yo._update(backend, cdn, tmp_path, yo.GAME, "2.1.0")
    assert (root / "data/old.pak").read_bytes() == b"player's own edit"
    assert not (root / "data/sub/gone.bin").exists()  # unchanged: removed as before
    assert "leftover_files" not in backend.status().data
    assert any("kept 1 file" in line and "data/old.pak" in line for line in lines)
    # A later update does not come back for it.
    yo._update(backend, cdn, tmp_path, yo.GAME, "2.2.0")
    assert (root / "data/old.pak").read_bytes() == b"player's own edit"


def test_yostar_records_what_it_installed(cdn, tmp_path):
    from nadare.core import yostar

    backend = yo._install(cdn, tmp_path, yo.OLD)
    record = json.loads((backend.install_dir() / INSTALLED).read_text(encoding="utf-8"))
    assert record["game"] == "Arknights_EN" and record["version"] == "2.0.0"
    for path, data in yo.OLD.items():
        assert record["files"][path] == {"size": len(data), "crc64": yostar.crc64(data)}


def test_yostar_a_dropped_file_of_the_same_size_but_other_bytes_stays(cdn, tmp_path):
    backend = yo._install(cdn, tmp_path, yo.OLD)
    root = backend.install_dir()
    edited = b"X" * len(yo.OLD["data/old.pak"])
    (root / "data/old.pak").write_bytes(edited)
    yo._update(backend, cdn, tmp_path, yo.GAME, "2.1.0")
    assert (root / "data/old.pak").read_bytes() == edited


def test_yostar_a_damaged_record_falls_back_to_the_name(cdn, tmp_path):
    """No usable record says nothing about the file: the earlier behaviour stays."""
    backend = yo._install(cdn, tmp_path, yo.OLD)
    root = backend.install_dir()
    (root / INSTALLED).write_text("{not json", encoding="utf-8")
    yo._update(backend, cdn, tmp_path, yo.GAME, "2.1.0")
    assert not (root / "data/old.pak").exists()
    assert json.loads((root / INSTALLED).read_text(encoding="utf-8"))["version"] == "2.1.0"


# --------------------------------------------------------------------------- BPSR
def test_bpsr_keeps_a_dropped_file_the_player_changed(transport, tmp_path):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/old.pak": b"old", "BPSR_Data/gone.pak": b"gone"}
    v2 = {"BPSR.exe": b"MZ"}
    bpsr._publish(transport, tree, "1.0.0", v1)
    b = bpsr._backend(tmp_path)
    b.install()
    root = b.install_dir()
    (root / "BPSR_Data/old.pak").write_bytes(b"mine")
    bpsr._publish(transport, tree, "1.1.0", v2)
    b.update()
    assert (root / "BPSR_Data/old.pak").read_bytes() == b"mine"
    assert not (root / "BPSR_Data/gone.pak").exists()
    assert "leftover_files" not in b.status().data
    b.repair()
    assert (root / "BPSR_Data/old.pak").read_bytes() == b"mine"


# --------------------------------------------------------------------------- Kuro
def _record_installed(backend, root, paths):
    """What a finished sync of the previous version left: its files and their md5s."""
    import hashlib

    files = tuple(
        dd.RemoteFile(
            p,
            "https://x/" + p,
            (root / p).stat().st_size,
            hashlib.md5((root / p).read_bytes()).hexdigest(),
        )
        for p in paths
    )
    backend._remove_retired(dd.Plan(game_tag="wuwa", version="3.7.2", files=files), ())


def test_kuro_delete_files_keep_a_file_the_player_changed(wuwa):
    backend, root, fetch, lines = wuwa
    _record_installed(backend, root, kuro.RETIRED)
    (root / kuro.RETIRED[0]).write_bytes(b"player's mod")
    plan = backend._load_manifest()
    backend._remove_retired(plan, (kuro.RETIRED[0], kuro.RETIRED[2]))
    assert (root / kuro.RETIRED[0]).read_bytes() == b"player's mod"
    assert not (root / kuro.RETIRED[2]).exists()
    assert not (root / kuro.RETIRED[1]).exists()  # the older lists still name it: unchanged
    assert "leftover_files" not in backend.status().data
    assert any("kept 1 file" in line for line in lines)


def test_kuro_the_tidy_keeps_it_too_and_still_marks_the_version(wuwa):
    backend, root, fetch, lines = wuwa
    _record_installed(backend, root, kuro.RETIRED)
    (root / kuro.RETIRED[0]).write_bytes(b"player's mod")
    backend._remove_retired(backend._load_manifest(), ())
    (root / ".nadare" / "tidied-3.8.0").unlink(missing_ok=True)
    backend.check_update()
    assert (root / kuro.RETIRED[0]).read_bytes() == b"player's mod"
    assert (root / ".nadare" / "tidied-3.8.0").exists()


def test_kuro_without_a_record_the_name_decides_as_before(wuwa):
    backend, root, fetch, lines = wuwa
    (root / kuro.RETIRED[0]).write_bytes(b"changed, but nothing recorded it")
    backend._remove_retired(backend._load_manifest(), ())
    assert kuro._left(root) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"game": "other", "version": "1", "files": {}},
        {"game": "wuwa", "version": "1", "files": {"../../x": {"size": 1, "md5": "0" * 32}}},
        {"game": "wuwa", "version": "1", "files": {"a": {"size": True, "md5": "0" * 32}}},
        {"game": "wuwa", "version": "1", "files": {"a": {"size": 1, "crc64": "zz"}}},
        {"game": "wuwa", "version": "1", "files": []},
    ],
)
def test_kuro_a_record_it_cannot_trust_protects_nothing(wuwa, bad):
    backend, root, fetch, lines = wuwa
    (root / INSTALLED).parent.mkdir(parents=True, exist_ok=True)
    (root / INSTALLED).write_text(json.dumps(bad), encoding="utf-8")
    backend._remove_retired(backend._load_manifest(), ())
    assert kuro._left(root) == []


# ------------------------------------------- a dropped file nothing named yet (sim seed 1208)
def _plan(root, paths, version):
    import hashlib

    return dd.Plan(
        game_tag="wuwa",
        version=version,
        files=tuple(
            dd.RemoteFile(
                p, "", (root / p).stat().st_size, hashlib.md5((root / p).read_bytes()).hexdigest()
            )
            for p in paths
        ),
    )


def test_the_record_remembers_a_dropped_file_still_on_disk(tmp_path):
    """v2 drops a file but no list names it, so it stays. v3's older lists name it; by then the
    player changed it. The record written after v2 must still know it, or v3 removes it."""
    from nadare.core import installedfiles

    (tmp_path / "keep.pak").write_bytes(b"kept")
    (tmp_path / "old.dll").write_bytes(b"nadare's")
    installedfiles.write(tmp_path, "wuwa", _plan(tmp_path, ["keep.pak", "old.dll"], "1"), {})
    installedfiles.write(tmp_path, "wuwa", _plan(tmp_path, ["keep.pak"], "2"), {})
    (tmp_path / "old.dll").write_bytes(b"player's mod")
    removable, kept = installedfiles.split_changed(
        tmp_path, ["old.dll"], installedfiles.read(tmp_path, "wuwa")
    )
    assert removable == [] and list(kept) == ["old.dll"]


def test_the_record_forgets_a_dropped_file_once_it_is_gone(tmp_path):
    from nadare.core import installedfiles

    (tmp_path / "keep.pak").write_bytes(b"kept")
    (tmp_path / "old.dll").write_bytes(b"nadare's")
    installedfiles.write(tmp_path, "wuwa", _plan(tmp_path, ["keep.pak", "old.dll"], "1"), {})
    (tmp_path / "old.dll").unlink()
    installedfiles.write(tmp_path, "wuwa", _plan(tmp_path, ["keep.pak"], "2"), {})
    assert set(installedfiles.read(tmp_path, "wuwa")) == {"keep.pak"}


def test_a_recorded_file_that_cannot_be_read_is_kept(tmp_path, monkeypatch):
    """Unreadable means not proven Nadare's: it stays rather than being removed by name.
    (A real chmod 000 does not stop root, so the read failure is simulated.)"""
    from nadare.core import installedfiles

    (tmp_path / "old.dll").write_bytes(b"nadare's")
    installedfiles.write(tmp_path, "wuwa", _plan(tmp_path, ["old.dll"], "1"), {})
    real_open = open

    def refuse(path, *args, **kwargs):
        if Path(path).name == "old.dll":
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(installedfiles, "open", refuse, raising=False)
    monkeypatch.setattr(installedfiles, "crc64_file", lambda p: refuse(p), raising=False)
    removable, kept = installedfiles.split_changed(
        tmp_path, ["old.dll"], installedfiles.read(tmp_path, "wuwa")
    )
    assert removable == [] and list(kept) == ["old.dll"]
