# SPDX-License-Identifier: GPL-3.0-or-later
"""Kuro leftover cleanup that cannot silently give up (support case 2026-11-13, #1252).

A WuWa player updated 3.7.2 -> 3.8.0 with Nadare 0.1.0b21 (whole files, retired files left on
disk), then ran b23: its automatic cleanup did nothing and said nothing, and the game showed
"Fatal error". Now: the cleanup marks a version done only after every Kuro list was read and
every retired file is gone; each outcome is a visible log line; retired files it could not
remove are remembered and the game page tells the player to press Repair files.
"""

from __future__ import annotations

import json

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch, process

ROOT = "https://cdn.example/"
LISTS = {
    f"{ROOT}p/3.7.2.json": {
        "deleteFiles": ["Client/Content/Paks/old-a.pak", "Client/Content/Paks/old-a.sig"]
    },
    f"{ROOT}p/3.7.1.json": {"deleteFiles": ["Client/Content/Paks/old-b.pak"]},
}
EXTRA = {kuropatch.EXTRA_KEY: {"root": ROOT, "packs": {"common": [
    {"version": "3.7.2", "indexFile": "p/3.7.2.json", "baseUrl": "x/"},
    {"version": "3.7.1", "indexFile": "p/3.7.1.json", "baseUrl": "x/"},
]}}}
RETIRED = [
    "Client/Content/Paks/old-a.pak",
    "Client/Content/Paks/old-a.sig",
    "Client/Content/Paks/old-b.pak",
]
CURRENT = dd.RemoteFile("Client/Content/Paks/new.pak", "https://x/new.pak", 1, "0" * 32)


class Fetch:
    def __init__(self):
        self.broken: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if url in self.broken or url not in LISTS:
            raise dd.DirectError(f"unreadable {url}")
        return json.dumps(LISTS[url]).encode()


@pytest.fixture()
def wuwa(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "pfx")})
    backend = direct.WutheringWavesBackend(ctx)
    root = backend.install_dir()
    for rel in RETIRED + [CURRENT.path, "Client/Content/Paks/~mods/mine.pak", "Client/mystery.dat"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    (root / "Wuthering Waves.exe").write_bytes(b"MZ")
    target = backend.direct_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    backend._write_version_file("3.8.0", "wuwa")
    # As in Kuro's real list, the game's executables are current files of the plan.
    exes = tuple(
        dd.RemoteFile(p.relative_to(root).as_posix(), "https://x/exe", 2, "0" * 32)
        for p in (root / "Wuthering Waves.exe", target)
    )
    plan = dd.Plan(game_tag="wuwa", version="3.8.0", files=(CURRENT, *exes), extra=EXTRA)
    backend._load_manifest = lambda: plan
    fetch = Fetch()
    monkeypatch.setattr(direct, "patch_list_fetch", fetch)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    lines: list[str] = []
    monkeypatch.setattr(backend, "info", lambda text: lines.append(str(text)))
    return backend, root, fetch, lines


def _left(root):
    return [r for r in RETIRED if (root / r).exists()]


# ----------------------------------------------- the scan says what it could not read
def test_the_scan_reports_unreadable_lists(wuwa):
    backend, root, fetch, _ = wuwa
    fetch.broken.add(f"{ROOT}p/3.7.1.json")
    scan = kuropatch.retired_scan(backend._load_manifest(), root, fetch)
    assert set(scan.names) == {"Client/Content/Paks/old-a.pak", "Client/Content/Paks/old-a.sig"}
    assert scan.unreadable == ("3.7.1",)
    assert scan.complete is False


def test_a_complete_scan(wuwa):
    backend, root, fetch, _ = wuwa
    scan = kuropatch.retired_scan(backend._load_manifest(), root, fetch)
    assert set(scan.names) == set(RETIRED) and scan.unreadable == () and scan.complete
    # The old name keeps answering just the names.
    assert set(kuropatch.retired_on_disk(backend._load_manifest(), root, fetch)) == set(RETIRED)


# --------------------------------------------------------------- the automatic cleanup
def test_the_check_removes_them_says_so_and_marks_the_version(wuwa):
    backend, root, fetch, lines = wuwa
    backend.check_update()
    assert _left(root) == []
    assert any("removed 3 file(s)" in line for line in lines)
    assert (root / ".nadare" / "tidied-3.8.0").exists()
    # Mods and unknown files are never touched.
    assert (root / "Client/Content/Paks/~mods/mine.pak").exists()
    assert (root / "Client/mystery.dat").exists()


def test_an_unreadable_list_is_not_marked_done_and_is_retried(wuwa):
    backend, root, fetch, lines = wuwa
    fetch.broken.add(f"{ROOT}p/3.7.1.json")
    backend.check_update()
    assert _left(root) == ["Client/Content/Paks/old-b.pak"]  # what it could read, it removed
    assert not (root / ".nadare" / "tidied-3.8.0").exists()
    assert any("could not read" in line and "try again" in line for line in lines)
    fetch.broken.clear()
    backend.check_update()  # the next check tries again
    assert _left(root) == []
    assert (root / ".nadare" / "tidied-3.8.0").exists()


def test_a_file_it_cannot_delete_is_remembered_and_shown(wuwa, monkeypatch):
    backend, root, fetch, lines = wuwa
    real = kuropatch.delete_retired

    def stubborn(base, names):
        return real(base, tuple(n for n in names if not n.endswith("old-a.sig")))

    monkeypatch.setattr(kuropatch, "delete_retired", stubborn)
    backend.check_update()
    assert _left(root) == ["Client/Content/Paks/old-a.sig"]
    assert not (root / ".nadare" / "tidied-3.8.0").exists()
    assert backend.status().data.get("leftover_files") == 1
    assert any("Press Repair files" in line for line in lines)


def test_leftovers_found_while_the_game_is_busy_are_shown(wuwa):
    backend, root, fetch, lines = wuwa
    lock = process.OperationLock(backend.ctx.name)
    lock.acquire()
    try:
        backend.check_update()
    finally:
        lock.release()
    assert len(_left(root)) == 3  # nothing deleted under another operation
    assert not (root / ".nadare" / "tidied-3.8.0").exists()
    assert backend.status().data.get("leftover_files") == 3
    assert any("postponed" in line for line in lines)


def test_no_leftovers_means_nothing_on_the_page(wuwa):
    backend, root, fetch, _ = wuwa
    backend.check_update()
    assert "leftover_files" not in backend.status().data


def test_a_repair_removes_them_and_clears_the_hint(wuwa, monkeypatch):
    backend, root, fetch, _ = wuwa
    lock = process.OperationLock(backend.ctx.name)
    lock.acquire()
    try:
        backend.check_update()
    finally:
        lock.release()
    assert backend.status().data.get("leftover_files") == 3
    monkeypatch.setattr(dd, "sync", lambda *a, **k: {"downloaded": 0, "skipped": 1})
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None, raising=False)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None, raising=False)
    backend.update()  # Repair and Update both sync, then remove retired files
    assert _left(root) == []
    assert not (root / ".nadare" / "leftovers.json").exists()
    assert "leftover_files" not in backend.status().data


def test_a_leftover_count_for_another_version_is_ignored(wuwa):
    backend, root, fetch, _ = wuwa
    state = root / ".nadare" / "leftovers.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"version": "3.7.2", "names": ["Client/Content/Paks/old-a.pak"]}))
    assert "leftover_files" not in backend.status().data


# --------------------------------------------------------------- the game page
def test_the_game_page_tells_the_player_to_repair(tmp_path, qapp):
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_status(Status(state="installed", version="3.8.0", data={"leftover_files": 56}))
        text = view.detail_label.text()
        assert "56" in text and "Repair files" in text and view.detail_label.isVisibleTo(view)
        view.set_status(Status(state="installed", version="3.8.0", data={}))
        assert "Repair files" not in view.detail_label.text()
    finally:
        view.deleteLater()


def test_a_broken_list_that_no_longer_matters_does_not_block_the_mark(wuwa):
    backend, root, fetch, _ = wuwa
    # Only 3.7.1's file is left; the NEWER 3.7.2 list (read first) is broken, the older one
    # explains everything, so the broken list no longer matters.
    for rel in (*RETIRED[:2], "Client/mystery.dat"):
        (root / rel).unlink()
    fetch.broken.add(f"{ROOT}p/3.7.2.json")
    scan = kuropatch.retired_scan(backend._load_manifest(), root, fetch)
    assert scan.complete
    backend.check_update()
    assert (root / ".nadare" / "tidied-3.8.0").exists()


def test_a_remembered_leftover_that_is_gone_shows_nothing(wuwa):
    backend, root, fetch, _ = wuwa
    backend._write_leftover_state("3.8.0", ["Client/Content/Paks/old-a.pak"])
    assert backend.status().data.get("leftover_files") == 1
    (root / "Client/Content/Paks/old-a.pak").unlink()  # removed by hand
    assert "leftover_files" not in backend.status().data


def test_with_an_unknown_file_a_broken_list_still_matters(wuwa):
    """The broken list might name the unknown file: not done, retried at the next check."""
    backend, root, fetch, _ = wuwa
    (root / "Client/Content/Paks/old-b.pak").unlink()
    fetch.broken.add(f"{ROOT}p/3.7.1.json")
    assert kuropatch.retired_scan(backend._load_manifest(), root, fetch).complete is False
    assert (root / "Client/mystery.dat").exists()  # and an unknown file is never deleted


def test_nadares_own_files_are_never_candidates(wuwa):
    """The version record at the top of the folder is Nadare's, not an unknown game file."""
    backend, root, fetch, _ = wuwa
    assert (root / ".nadare-version.json").exists()
    (root / ".nadare-dl").mkdir()
    (root / ".nadare-dl" / "part.bin").write_bytes(b"x")
    found = kuropatch._leftovers(backend._load_manifest(), root)
    assert ".nadare-version.json" not in found
    assert not any(name.startswith(".nadare") for name in found)
