# SPDX-License-Identifier: GPL-3.0-or-later
"""#1557: the CZN updater runs on the shared sync engine (``core.directdl``).

The plan comes from STOVE's dpms manifest (one gzip chunk per client file, md5 of the packed
and of the unpacked bytes), :func:`directdl.sync` downloads it, the install lock is the one every
direct sync takes, and "the game is running" is the backend's own rule.

UPGRADE (release gate): an install the b27-and-older updater left must be recognised as it is.
``_old_install`` below writes exactly what that updater wrote (``git show
origin/b27:src/nadare/backends/czn/update.py``): the files at ``<game>/<path>``, the game's
``GameManifest_<service>.upf`` with ``local_version`` (``_write_local_version``, indent 4), the
manifest copy for Verify (``_cache_manifest``), the stat cache of verified files
(``_write_stat_cache``, schema 1, keys are the manifest paths with their leading slash) and,
for an interrupted download, ``<file>.part.gz`` (packed, resumable) and ``<file>.part``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _czn_test_helpers import (  # noqa: F401 - pytest fixtures, found by name
    _no_runner_downloads,
    _no_webview2_index,
    _reset_czn_process_state,
)
from _httpserver import Server

from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import CznBackend
from nadare.backends.czn.backend_parts import launching
from nadare.core import directdl

SERVICE = czn_update.SERVICE
DPMS = f"/game/dpms_{SERVICE}"


def _blob(name: str, size: int) -> bytes:
    return hashlib.shake_256(name.encode()).digest(size)


# Two published versions: v57 changes the shield only (as STOVE's real 56 -> 57 did).
V56 = {
    "/bin/ssr-stove-shield.exe": ("56", "1", _blob("shield-56", 200_000)),
    "/bin/BaseSDK.dll": ("33", "1", _blob("basesdk", 120_000)),
    "/bin/crashpad_handler.exe": ("32", "2", _blob("crashpad", 50_000)),
    "/bin/custodia/sparrow_x64.smt": ("51", "2", _blob("sparrow", 30_000)),
}
V57 = dict(V56, **{"/bin/ssr-stove-shield.exe": ("57", "1", _blob("shield-57", 210_000))})


def _manifest(files: dict, version: int) -> dict:
    rows = ["1 | D | /bin", "2 | D | /bin/custodia", "3 | D | /bin/custodiap"]
    for index, (path, (vf, ci, data)) in enumerate(files.items(), start=4):
        packed = gzip.compress(data, mtime=0)
        rows.append(
            f"{index} | F | {path} | {vf} | {ci} | gz | {len(data)} | {len(packed)} | "
            f"{hashlib.md5(data).hexdigest()} | {hashlib.md5(packed).hexdigest()} | a |  |"
        )
    rows.append(f"{len(rows) + 1} | G | /bin/custodiap/.keep")
    return {"version_no": version, "root_folder": "ChaosZero", "execution": "bin\\x.exe",
            "files": rows}


MANIFESTS = {56: _manifest(V56, 56), 57: _manifest(V57, 57)}


def _chunks(*versions: dict) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}
    for files in versions:
        for vf, ci, data in files.values():
            objects[f"{DPMS}/v{vf}/{ci}.gz"] = gzip.compress(data, mtime=0)
    return objects


@pytest.fixture
def czn(tmp_path, monkeypatch):
    """The updater's module paths pointed at a test prefix, and a local dpms CDN."""
    prefix = tmp_path / "prefix"
    game = prefix / "drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare"
    (prefix / "drive_c").mkdir(parents=True)
    monkeypatch.setattr(czn_update, "PREFIX", prefix)
    monkeypatch.setattr(czn_update, "GAME_DIR", game)
    monkeypatch.setattr(
        czn_update, "UPF", game / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"
    )
    monkeypatch.setattr(
        czn_update, "MANIFEST_CACHE",
        game / "combinedata_manifest" / f"nadare-manifest-{SERVICE}.json",
    )
    monkeypatch.setattr(czn_update, "STAT_CACHE", prefix / f"nadare-verified-files-{SERVICE}.json")
    server = Server(_chunks(V56, V57))
    monkeypatch.setattr(czn_update, "BASE", server.url + DPMS)
    monkeypatch.setattr(czn_update, "fetch_manifest", lambda version: MANIFESTS[version])
    monkeypatch.setattr(launching, "game_pids", lambda game_dir, helpers=True: [])
    yield prefix, game, server
    server.close()


def _latest(monkeypatch, version: int) -> None:
    monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (version, "index"))


def _old_install(prefix: Path, game: Path, files: dict, version: int, *, stat_cache=True):
    """What the b27-and-older updater left after ``update --apply`` to ``version``."""
    for path, (_vf, _ci, data) in files.items():
        target = game / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for folder in ("bin", "bin/custodia", "bin/custodiap"):
        (game / folder).mkdir(parents=True, exist_ok=True)
    upf = game / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"
    upf.parent.mkdir(parents=True, exist_ok=True)
    upf.write_text(json.dumps({"local_version": version, "game_id": SERVICE}, indent=4))
    (game / "combinedata_manifest" / f"nadare-manifest-{SERVICE}.json").write_text(
        json.dumps(MANIFESTS[version])
    )
    if stat_cache:
        rows = {}
        for path, (_vf, _ci, data) in files.items():
            info = (game / path.lstrip("/")).stat()
            rows[path] = {
                "md5": hashlib.md5(data).hexdigest(),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "ino": info.st_ino,
                "dev": info.st_dev,
            }
        (prefix / f"nadare-verified-files-{SERVICE}.json").write_text(
            json.dumps({"schema": 1, "files": rows}, separators=(",", ":"))
        )


def _chunk_requests(server: Server) -> list[str]:
    return [r for r in server.requests if r.endswith(".gz")]


# ---------------------------------------------------------------------------
# UPGRADE: an install the old updater made
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stat_cache", [True, False], ids=["with-stat-cache", "no-stat-cache"])
def test_an_old_install_is_recognised_and_nothing_is_downloaded(
    czn, monkeypatch, capsys, stat_cache
):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56, stat_cache=stat_cache)
    _latest(monkeypatch, 56)
    assert czn_update.local_version() == 56
    assert czn_update.main(["update", "--apply"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert _chunk_requests(server) == []
    assert czn_update.local_version() == 56


def test_repair_of_an_intact_old_install_downloads_nothing(czn, monkeypatch):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    _latest(monkeypatch, 56)
    assert czn_update.main(["update", "--apply", "--full"]) == 0
    assert _chunk_requests(server) == []


def test_the_backend_reads_the_old_version_marker(czn, tmp_path):
    prefix, game, _server = czn
    _old_install(prefix, game, V56, 56)
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(prefix)}))
    status = backend.status()
    assert status.state == "installed"
    assert status.version == "v56"
    assert backend._installed_number() == 56


def test_verify_of_an_old_install_uses_its_cached_manifest(czn, capsys):
    prefix, game, _server = czn
    _old_install(prefix, game, V56, 56)
    assert czn_update.main(["verify"]) == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event == {"event": "verify", "checked": True, "version": 56, "count": 0, "differ": []}


def test_an_update_of_an_old_install_fetches_only_the_changed_file(czn, monkeypatch):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    untouched = game / "bin/BaseSDK.dll"
    before = untouched.stat()
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    assert _chunk_requests(server) == [f"{DPMS}/v57/1.gz"]
    assert server.bytes_served == len(gzip.compress(V57["/bin/ssr-stove-shield.exe"][2], mtime=0))
    assert (game / "bin/ssr-stove-shield.exe").read_bytes() == V57["/bin/ssr-stove-shield.exe"][2]
    after = untouched.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert czn_update.local_version() == 57
    upf = json.loads(czn_update.UPF.read_text())
    assert upf == {"local_version": 57, "game_id": SERVICE}
    assert json.loads(czn_update.MANIFEST_CACHE.read_text())["version_no"] == 57
    # A second run finds everything current.
    server.requests.clear()
    assert czn_update.main(["update", "--apply"]) == 0
    assert _chunk_requests(server) == []


def test_an_old_partial_download_is_resumed_and_its_staging_removed(czn, monkeypatch):
    """The old updater's ``<file>.part.gz`` holds the first bytes of the chunk: they are kept."""
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    new_packed = gzip.compress(V57["/bin/ssr-stove-shield.exe"][2], mtime=0)
    half = len(new_packed) // 2
    shield = game / "bin/ssr-stove-shield.exe"
    Path(f"{shield}.part.gz").write_bytes(new_packed[:half])
    Path(f"{shield}.part").write_bytes(b"half an unpacked file")
    # An interrupted download of a file that is current by now: only litter.
    Path(f"{game}/bin/BaseSDK.dll.part.gz").write_bytes(b"old bytes")
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    assert server.ranges == [(f"{DPMS}/v57/1.gz", half, len(new_packed) - 1)]
    assert server.bytes_served == len(new_packed) - half
    assert shield.read_bytes() == V57["/bin/ssr-stove-shield.exe"][2]
    leftovers = sorted(p.name for p in (game / "bin").iterdir() if p.is_file())
    assert leftovers == ["BaseSDK.dll", "crashpad_handler.exe", "ssr-stove-shield.exe"]


def test_a_part_file_that_is_not_the_old_updaters_is_left_alone(czn, monkeypatch):
    prefix, game, _server = czn
    _old_install(prefix, game, V56, 56)
    mine = game / "bin/notes.txt.part.gz"
    mine.write_bytes(b"not a manifest file's staging")
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    assert mine.read_bytes() == b"not a manifest file's staging"


# ---------------------------------------------------------------------------
# fresh install, repair
# ---------------------------------------------------------------------------
def test_a_fresh_install_into_an_existing_prefix(czn, monkeypatch):
    _prefix, game, server = czn
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    for path, (_vf, _ci, data) in V57.items():
        assert (game / path.lstrip("/")).read_bytes() == data
    assert (game / "bin/custodiap").is_dir()
    assert len(_chunk_requests(server)) == len(V57)
    assert czn_update.local_version() == 57


def test_repair_fetches_only_the_damaged_files(czn, monkeypatch):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    (game / "bin/BaseSDK.dll").write_bytes(b"corrupt")
    (game / "bin/custodia/sparrow_x64.smt").unlink()
    _latest(monkeypatch, 56)
    assert czn_update.main(["update", "--apply", "--full"]) == 0
    assert sorted(_chunk_requests(server)) == [f"{DPMS}/v33/1.gz", f"{DPMS}/v51/2.gz"]
    assert (game / "bin/BaseSDK.dll").read_bytes() == V56["/bin/BaseSDK.dll"][2]
    assert (game / "bin/custodia/sparrow_x64.smt").is_file()


def test_the_stove_headers_go_with_every_chunk(czn, monkeypatch):
    _prefix, _game, server = czn
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    chunk_headers = [h for r, h in zip(server.requests, server.headers, strict=True)
                     if r.endswith(".gz")]
    assert chunk_headers
    for headers in chunk_headers:
        assert headers["caller-id"] == czn_update.HEADERS["caller-id"]
        assert headers["caller-detail"] == czn_update.HEADERS["caller-detail"]


# ---------------------------------------------------------------------------
# the shared guards
# ---------------------------------------------------------------------------
def test_a_running_game_stops_the_update_before_anything_is_written(czn, monkeypatch, capsys):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    seen: list[Path] = []

    def running(game_dir, helpers=True):
        seen.append(game_dir)
        return [4242]

    monkeypatch.setattr(launching, "game_pids", running)
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 3
    assert "running" in capsys.readouterr().out
    assert seen and seen[0] == game  # the backend's own rule, for this install
    assert _chunk_requests(server) == []
    assert czn_update.local_version() == 56


def test_the_backend_and_the_updater_share_one_running_rule(czn, tmp_path, monkeypatch):
    prefix, _game, _server = czn
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(prefix)}))
    monkeypatch.setattr(launching, "game_pids", lambda game_dir, helpers=True: [7])
    assert backend._game_running() and czn_update.game_running()
    monkeypatch.setattr(launching, "game_pids", lambda game_dir, helpers=True: [])
    assert not backend._game_running() and not czn_update.game_running()


def test_another_sync_into_the_folder_makes_the_update_wait(czn, monkeypatch, capsys):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    _latest(monkeypatch, 57)
    with directdl.install_lock(game):
        # Its own exit code: the GUI must not call a held lock "a download failed".
        assert czn_update.main(["update", "--apply"]) == czn_update.EXIT_BUSY == 6
    assert "already running" in capsys.readouterr().out
    assert _chunk_requests(server) == []
    assert czn_update.local_version() == 56


def test_a_held_lock_on_an_up_to_date_install_is_busy_too(czn, monkeypatch, capsys):
    """The marker-only path (files already match) reports the same busy code."""
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    _latest(monkeypatch, 56)
    monkeypatch.setattr(czn_update, "local_version", lambda: 55)  # marker behind the files
    with directdl.install_lock(game):
        assert czn_update.main(["update", "--apply"]) == czn_update.EXIT_BUSY
    assert "already running" in capsys.readouterr().out
    assert _chunk_requests(server) == []


def test_the_backend_words_the_updater_codes_that_are_not_failures():
    from nadare.backends.czn.backend import UPDATE_EXIT_MESSAGES

    busy = UPDATE_EXIT_MESSAGES[czn_update.EXIT_BUSY]
    assert "already running" in busy and "download" not in busy
    running = UPDATE_EXIT_MESSAGES[czn_update.EXIT_RUNNING]
    assert "running" in running and "close" in running.lower()
    assert czn_update.EXIT_RUNNING == 3  # the documented code, kept


def test_the_lock_covers_the_version_marker(czn, monkeypatch):
    """The marker is written while the lock is still held (no second sync in between)."""
    prefix, game, _server = czn
    _old_install(prefix, game, V56, 56)
    _latest(monkeypatch, 57)
    held: list[bool] = []
    real = czn_update._write_local_version

    def check(manifest, target):
        try:
            with directdl.install_lock(game):
                held.append(False)
        except directdl.InstallBusy:
            held.append(True)
        real(manifest, target)

    monkeypatch.setattr(czn_update, "_write_local_version", check)
    assert czn_update.main(["update", "--apply"]) == 0
    assert held == [True]


def test_a_chunk_with_the_wrong_md5_changes_nothing(czn, monkeypatch, capsys):
    prefix, game, server = czn
    _old_install(prefix, game, V56, 56)
    server.objects[f"{DPMS}/v57/1.gz"] = gzip.compress(b"tampered" * 1000, mtime=0)
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 1
    assert "mismatch" in capsys.readouterr().out
    assert (game / "bin/ssr-stove-shield.exe").read_bytes() == V56["/bin/ssr-stove-shield.exe"][2]
    assert czn_update.local_version() == 56


def test_a_manifest_row_without_a_valid_md5_is_refused(czn, monkeypatch, capsys):
    _prefix, game, server = czn
    bad = _manifest(V57, 57)
    bad["files"] = [row.replace(row.split("|")[9], " nothex ") if "| F |" in row else row
                    for row in bad["files"]]
    monkeypatch.setattr(czn_update, "fetch_manifest", lambda version: bad)
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 2
    assert "rejected" in capsys.readouterr().out
    assert _chunk_requests(server) == []


def test_a_chunk_name_that_is_not_a_plain_token_is_refused(czn, monkeypatch, capsys):
    _prefix, _game, server = czn
    bad = _manifest({"/bin/a.dll": ("57", "../../x", b"data")}, 57)
    monkeypatch.setattr(czn_update, "fetch_manifest", lambda version: bad)
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 2
    assert "rejected" in capsys.readouterr().out
    assert _chunk_requests(server) == []


def test_files_are_checked_with_the_shared_hashing(czn, monkeypatch):
    """The plan's on-disk check is directdl's, not a private md5 loop."""
    prefix, game, _server = czn
    _old_install(prefix, game, V56, 56, stat_cache=False)
    calls: list[Path] = []
    real = directdl._verify_remote

    def spy(path, rf, **kwargs):
        calls.append(Path(path))
        return real(path, rf, **kwargs)

    monkeypatch.setattr(directdl, "_verify_remote", spy)
    assert czn_update.plan(MANIFESTS[56], trust_stat_cache=False)[0] == []
    assert sorted(calls) == sorted(game / p.lstrip("/") for p in V56)


def test_progress_lines_reach_the_backend_as_a_fraction(czn, monkeypatch, capsys):
    _prefix, _game, _server = czn
    _latest(monkeypatch, 57)
    assert czn_update.main(["update", "--apply"]) == 0
    from nadare.backends.czn.backend_parts.streaming import _FILE_PROGRESS

    progress = [m for line in capsys.readouterr().out.splitlines()
                if (m := _FILE_PROGRESS.match(line))]
    assert progress
    done, total = int(progress[-1].group(1)), int(progress[-1].group(2))
    assert done == total > 0


def test_no_engine_of_its_own_is_left(czn):
    """The private fetch, md5 and /proc loops are gone (#1557)."""
    for name in ("_download_stream", "_fetch_verified_to_file", "download_file", "_md5_file",
                 "_cmdline_has_exe"):
        assert not hasattr(czn_update, name), name
    source = Path(czn_update.__file__).read_text(encoding="utf-8")
    assert "/proc" not in source
    assert "hashlib.md5" not in source
    assert os.path.basename(czn_update.__file__) == "update.py"
