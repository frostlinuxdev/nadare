# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 3b: Arknights and Stella Sora install through directdl.

The backends turn the Yostar manifest into a plan (``yostar.to_plan``) and hand it to
``directdl.sync``; nothing else about them changes. These tests run the real downloader
against a local CDN (no sync fakes), so they show what a player gets: the files, the
version record, the marker, and the messages when the host is refused or a download fails
before or after a file was replaced. Data a b25 sync left behind (a ``.part``) must not
get in the way.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import urllib.parse
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core import directdl, yostar
from nadare.core.backends import BackendError
from nadare.core.gameversion import read_version_record


@pytest.fixture(autouse=True)
def _offline_machinery(monkeypatch):
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(wg.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise wg.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(wg.runners, "ensure_pinned", offline)
    monkeypatch.setattr(wg.WineGameBackend, "_ensure_direct_prefix", lambda self: None)


class _Cdn:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.missing: set[str] = set()
        self.hits: list[str] = []


@pytest.fixture
def cdn():
    state = _Cdn()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            name = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            name = name.removeprefix("/assets/")
            state.hits.append(name)
            data = None if name in state.missing else state.files.get(name)
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0
            if self.headers.get("Range"):
                start = int(self.headers["Range"].split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
                )
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}/assets"
    yield state
    server.shutdown()
    server.server_close()


def _manifest(
    tmp_path: Path, tag: str, files: dict[str, bytes], base: str, version: str = "2.0.0"
) -> Path:
    payload = {
        "game_tag": tag,
        "version": version,
        "base_url": base,
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": path, "size": len(data), "crc64": yostar.crc64(data)}
            for path, data in files.items()
        ],
    }
    path = tmp_path / f"{tag}-{version}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


GAME = {"Arknights.exe": b"MZ-arknights", "data/big.pak": b"B" * 4096, "data/empty.bin": b""}
NEW = {"Arknights.exe": b"MZ-arknights-2", "data/big.pak": b"C" * 4096, "data/empty.bin": b""}


def _ak(tmp_path: Path, manifest: Path) -> ArknightsBackend:
    return ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"manifest_file": str(manifest)}, "ak")
    )


def _spy_sync(monkeypatch) -> list[directdl.Plan]:
    seen: list[directdl.Plan] = []
    real = directdl.sync

    def spy(plan, dest, **kwargs):
        seen.append(plan)
        return real(plan, dest, **kwargs)

    monkeypatch.setattr(directdl, "sync", spy)
    return seen


def test_arknights_installs_through_directdl(cdn, tmp_path: Path, monkeypatch) -> None:
    cdn.files.update(GAME)
    seen = _spy_sync(monkeypatch)
    monkeypatch.setattr(
        yostar, "sync", lambda *a, **k: pytest.fail("the Yostar downloader must not run")
    )
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.install()

    root = backend.install_dir()
    for path, data in GAME.items():
        assert (root / path).read_bytes() == data, path
    assert not backend._install_marker_path().exists()
    record = read_version_record(root)
    assert record["version"] == "2.0.0" and record["game_tag"] == "Arknights_EN"
    assert backend.status().state == "installed"
    assert backend.verify()["verified"] is True
    # The plan is the manifest's, guard and all.
    (plan,) = seen
    assert plan.game_tag == "Arknights_EN" and plan.version == "2.0.0"
    assert {rf.path for rf in plan.files} == set(GAME)
    assert all(rf.crc64 is not None for rf in plan.files)
    assert plan.redirect_allowed is not None


def test_stella_sora_installs_through_directdl(cdn, tmp_path: Path, monkeypatch) -> None:
    files = {"StellaSora.exe": b"MZ-stella", "data/a.bin": b"stella"}
    cdn.files.update(files)
    seen = _spy_sync(monkeypatch)
    manifest = _manifest(tmp_path, "StellaSora_EN", files, cdn.base)
    backend = StellaSoraBackend(
        make_context(
            tmp_path, StellaSoraBackend.kind, {"manifest_file": str(manifest)}, "stella"
        )
    )
    backend.install()
    for path, data in files.items():
        assert (backend.install_dir() / path).read_bytes() == data
    assert [plan.game_tag for plan in seen] == ["StellaSora_EN"]
    assert backend.status().state == "installed"


def test_an_update_replaces_changed_files_and_records_the_version(
    cdn, tmp_path: Path, monkeypatch
) -> None:
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.install()
    cdn.files.update(NEW)
    backend.ctx.profile["manifest_file"] = str(
        _manifest(tmp_path, "Arknights_EN", NEW, cdn.base, version="2.1.0")
    )
    cdn.hits.clear()
    backend.update()
    for path, data in NEW.items():
        assert (backend.install_dir() / path).read_bytes() == data
    assert read_version_record(backend.install_dir())["version"] == "2.1.0"
    # The empty file did not change: it is not fetched again.
    assert "data/empty.bin" not in cdn.hits
    assert not backend._install_marker_path().exists()


def test_an_untrusted_host_is_refused_before_anything_changes(tmp_path: Path, monkeypatch) -> None:
    seen = _spy_sync(monkeypatch)
    manifest = _manifest(tmp_path, "Arknights_EN", GAME, "https://evil.example/assets")
    backend = _ak(tmp_path, manifest)
    with pytest.raises(BackendError, match="Nothing was changed"):
        backend.install()
    assert seen == []
    assert not backend._install_marker_path().exists()
    assert backend.status().state == "not_installed"


def test_an_update_that_fails_before_replacing_anything_keeps_the_game_playable(
    cdn, tmp_path: Path
) -> None:
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.install()
    # The new version's only changed file is gone from the CDN: the download fails
    # before any installed file was replaced.
    changed = {**GAME, "data/big.pak": b"D" * 4096}
    cdn.missing.add("data/big.pak")
    backend.ctx.profile["manifest_file"] = str(
        _manifest(tmp_path, "Arknights_EN", changed, cdn.base, version="2.1.0")
    )
    with pytest.raises(BackendError, match="Nothing was changed"):
        backend.update()
    assert not backend._install_marker_path().exists()
    assert backend.status().state == "installed"
    assert (backend.install_dir() / "data/big.pak").read_bytes() == GAME["data/big.pak"]
    assert read_version_record(backend.install_dir())["version"] == "2.0.0"


def test_an_update_that_fails_after_replacing_a_file_keeps_the_marker(
    cdn, tmp_path: Path
) -> None:
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.install()
    # Two files change; one arrives, the other is missing: the install is now mixed.
    cdn.files["Arknights.exe"] = NEW["Arknights.exe"]
    cdn.missing.add("data/big.pak")
    backend.ctx.profile["manifest_file"] = str(
        _manifest(tmp_path, "Arknights_EN", NEW, cdn.base, version="2.1.0")
    )
    backend.ctx.profile["download_workers"] = 1
    with pytest.raises(BackendError, match="could not finish") as caught:
        backend.update()
    assert "Nothing was changed" not in str(caught.value)
    assert backend._install_marker_path().exists()
    assert backend.status().state == "not_installed"


def test_a_part_file_a_b25_sync_left_is_resumed_or_cleared(cdn, tmp_path: Path) -> None:
    """b25's Yostar downloader staged ``<file>.part`` in place; directdl must finish the
    job and leave no ``.part`` behind (upgrade: old data opened by the new code)."""
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    root = backend.install_dir()
    (root / "data").mkdir(parents=True)
    (root / "data/big.pak.part").write_bytes(GAME["data/big.pak"][:1000])
    # An interrupted b25 replacement of a finished file: stale bytes in its .part.
    (root / "Arknights.exe").write_bytes(b"MZ-old")
    (root / "Arknights.exe.part").write_bytes(b"garbage")
    backend._write_install_marker()
    backend.install()
    for path, data in GAME.items():
        assert (root / path).read_bytes() == data
    assert not list(root.rglob("*.part"))
    assert backend.status().state == "installed"


def test_verify_uses_the_crc64_through_directdl(cdn, tmp_path: Path, monkeypatch) -> None:
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.install()
    calls: list[directdl.Plan] = []
    real = directdl.plan_missing

    def spy(plan, dest, **kwargs):
        calls.append(plan)
        return real(plan, dest, **kwargs)

    monkeypatch.setattr(directdl, "plan_missing", spy)
    monkeypatch.setattr(
        yostar, "plan", lambda *a, **k: pytest.fail("verify must use directdl")
    )
    (backend.install_dir() / "data/big.pak").write_bytes(b"X" * 4096)  # same size
    report = backend.verify()
    assert report["verified"] is False
    assert "big.pak" in report["message"]
    assert calls and calls[0].game_tag == "Arknights_EN"


def test_another_sync_holding_the_folder_keeps_its_marker(cdn, tmp_path: Path) -> None:
    """A refused sync must not drop a marker another sync wrote in the meantime."""
    from nadare.core import installlock

    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    with installlock.hold(backend.install_dir(), RuntimeError, "held by a test"):
        with pytest.raises(BackendError, match="already running"):
            backend.install()
        # Our own run wrote the marker; the folder belongs to the other sync now, whose
        # marker this is as far as anyone can tell: it stays.
        assert backend._install_marker_path().exists()


def test_busy_is_its_own_error_but_an_unusable_lock_folder_is_not(
    tmp_path: Path, monkeypatch
) -> None:
    from nadare.core import installlock, paths

    with installlock.hold(tmp_path, RuntimeError, "held"):
        with pytest.raises(directdl.InstallBusy):
            with directdl._install_lock(tmp_path):
                pass
    blocked = tmp_path / "state-file"
    blocked.write_text("not a folder")
    monkeypatch.setattr(paths, "state_dir", lambda: blocked)
    with pytest.raises(directdl.DirectError) as caught:
        with directdl._install_lock(tmp_path / "other"):
            pass
    assert not isinstance(caught.value, directdl.InstallBusy)


def test_a_first_install_that_fails_says_to_continue_not_that_a_game_is_installed(
    cdn, tmp_path: Path
) -> None:
    cdn.files.update(GAME)
    cdn.missing.update(GAME)  # nothing arrives, so nothing was replaced
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    with pytest.raises(BackendError) as caught:
        backend.install()
    message = str(caught.value)
    assert "installed game is as it was" not in message
    assert "Install again" in message


def test_a_retry_that_fails_early_keeps_an_earlier_runs_marker(cdn, tmp_path: Path) -> None:
    """An interrupted install left half the files and its marker; a retry that fails before
    replacing anything must not drop that marker, or half a game would show as installed."""
    cdn.files.update(GAME)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    root = backend.install_dir()
    root.mkdir(parents=True)
    (root / "Arknights.exe").write_bytes(GAME["Arknights.exe"])  # half an install
    backend._write_install_marker()
    cdn.missing.update(GAME)
    with pytest.raises(BackendError):
        backend.install()
    assert backend._install_marker_path().exists()
    assert backend.status().state == "not_installed"


@pytest.mark.parametrize("workers", ["many", 0, -3, 99])
def test_an_odd_download_workers_value_still_installs(
    cdn, tmp_path: Path, monkeypatch, workers
) -> None:
    """b25 read the profile's value leniently (1-32 now, 6 when unreadable); so does b28.

    An unreadable value keeps the plan default, which is what the old 6 meant for a
    small manifest; the profile can no longer raise a plan's own worker count.
    """
    cdn.files.update(GAME)
    seen: list[int] = []
    real = directdl.sync

    def spy(plan, dest, **kwargs):
        seen.append(kwargs["workers"])
        return real(plan, dest, **kwargs)

    monkeypatch.setattr(directdl, "sync", spy)
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, cdn.base))
    backend.ctx.profile["download_workers"] = workers
    backend.install()
    assert backend.status().state == "installed"
    assert seen and 1 <= seen[0] <= 32


def test_yostar_uses_plan_default_and_profile_only_caps(tmp_path, monkeypatch):
    backend = _ak(tmp_path, _manifest(tmp_path, "Arknights_EN", GAME, "http://127.0.0.1"))
    seen = []
    monkeypatch.setattr(directdl, "plan_workers", lambda plan: 24)
    monkeypatch.setattr(
        directdl, "sync",
        lambda *a, **kw: seen.append(kw["workers"]) or {"downloaded": 0, "skipped": 0},
    )
    backend.install()
    backend.ctx.profile["download_workers"] = 32
    backend.install()
    backend.ctx.profile["download_workers"] = 8
    backend.install()
    assert seen == [24, 24, 8]
