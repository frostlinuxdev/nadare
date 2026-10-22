# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for overnight-audit findings C-01..C-05 and C-07."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from nadare.core import download, shortcuts, yostar
from nadare.core.config import Config


# ---- C-01 -------------------------------------------------------------------
@pytest.mark.parametrize("profiles", [["a"], "oops", 3, {"game": "nope"}])
def test_wrong_typed_profiles_are_backed_up(tmp_path: Path, profiles) -> None:
    path = tmp_path / "config.json"
    original = json.dumps({"profiles": profiles})
    path.write_text(original, encoding="utf-8")

    cfg = Config.load(path)

    assert cfg.notice
    backups = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    cfg.save()
    assert backups[0].read_text(encoding="utf-8") == original


# ---- C-02 -------------------------------------------------------------------
def test_removal_in_another_process_is_not_resurrected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("one", backend="script")
    first.add_profile("two", backend="script")
    first.save()

    other = Config.load(path)  # a second live process
    other.remove_profile("two")
    other.save()

    first.data["language"] = "fr"
    first.save()

    disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(disk["profiles"]) == {"one"}
    assert disk["language"] == "fr"


def test_unchanged_top_level_settings_do_not_revert_other_processes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("one", backend="script")
    first.add_profile("two", backend="script")
    first.data["active_profile"] = "one"
    first.save()

    other = Config.load(path)
    other.data["active_profile"] = "two"
    other.save()

    first.add_profile("three", backend="script")  # does not touch active_profile
    first.data["active_profile"] = "one"  # add_profile made "three" active; restore ours
    first._top_snapshot["active_profile"] = "one"
    first.save()

    disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(disk["profiles"]) == {"one", "two", "three"}
    assert disk["active_profile"] == "two"


# ---- C-03 -------------------------------------------------------------------
@pytest.fixture
def partial_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = b"x" * 10
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", "bytes 0-9/100")
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/f"
    httpd.shutdown()
    httpd.server_close()


def test_download_rejects_unrequested_206(partial_server, tmp_path: Path) -> None:
    dest = tmp_path / "f.bin"
    with pytest.raises(download.DownloadError):
        download.download(partial_server, dest, retries=1, backoff=0)
    assert not dest.exists()


def test_stream_download_rejects_unrequested_206(partial_server, tmp_path: Path) -> None:
    pool = download.HttpClientPool()
    try:
        with pytest.raises(download.DownloadError, match="206"):
            download.stream_download(partial_server, tmp_path / "f.bin", pool=pool)
    finally:
        pool.close()
    assert not (tmp_path / "f.bin").exists()


# ---- C-04 -------------------------------------------------------------------
def test_second_sync_into_the_same_tree_is_refused(tmp_path: Path) -> None:
    with yostar._install_lock(tmp_path):
        with pytest.raises(yostar.ManifestError, match="already running"):
            with yostar._install_lock(tmp_path):
                pass
    with yostar._install_lock(tmp_path):  # released afterwards
        pass


# ---- C-07 -------------------------------------------------------------------
def test_remove_steam_shortcut_only_drops_that_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    argv = ["/opt/nadare.AppImage"]
    shortcuts.add_steam_shortcut(vdf, "one", "One", argv)
    shortcuts.add_steam_shortcut(vdf, "two", "Two", argv)

    assert shortcuts.remove_steam_shortcut(vdf, "one") is True
    assert shortcuts.remove_steam_shortcut(vdf, "one") is False
    names = [e["AppName"] for e in shortcuts.vdf_loads(vdf.read_bytes(), strict=True)]
    assert names == ["Two"]


def test_remove_steam_shortcut_refuses_while_steam_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    shortcuts.add_steam_shortcut(vdf, "one", "One", ["/opt/n"])
    before = vdf.read_bytes()
    monkeypatch.setattr(shortcuts, "steam_running", lambda: True)
    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.remove_steam_shortcut(vdf, "one")
    assert vdf.read_bytes() == before


def test_removing_a_game_removes_its_steam_entry(tmp_path: Path, monkeypatch) -> None:
    from nadare.core import removal

    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    shortcuts.add_steam_shortcut(vdf, "one", "One", ["/opt/n"])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("one", backend="script")

    result = removal.remove_game(
        cfg, "one", applications_dir=tmp_path / "a", desktop_dir=tmp_path / "d"
    )

    assert vdf in result.shortcuts_removed
    assert shortcuts.vdf_loads(vdf.read_bytes(), strict=True) == []


# ---- flash sweep: "delete Nadare's data" must keep the game ------------------
def test_deleting_data_keeps_a_default_install_and_prefix(tmp_path: Path) -> None:
    from nadare.core import paths, removal

    data = paths.profiles_dir() / "one"
    (data / "install").mkdir(parents=True)
    (data / "install" / "game.exe").write_text("x")
    (data / "prefix" / "drive_c").mkdir(parents=True)
    (data / "logs").mkdir()
    (data / "logs" / "a.log").write_text("log")
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(
        "one",
        backend="script",
        install_dir=str(data / "install"),
        prefix=str(data / "prefix"),
    )

    result = removal.remove_game(
        cfg, "one", delete_data=True, applications_dir=tmp_path / "a", desktop_dir=tmp_path / "d"
    )

    assert (data / "install" / "game.exe").is_file()
    assert (data / "prefix" / "drive_c").is_dir()
    assert not (data / "logs").exists()
    assert any("kept the game files" in w for w in result.warnings)


def test_deleting_data_and_ticked_trees_removes_everything(tmp_path: Path) -> None:
    from nadare.core import paths, removal

    data = paths.profiles_dir() / "one"
    (data / "install").mkdir(parents=True)
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("one", backend="script", install_dir=str(data / "install"))

    removal.remove_game(
        cfg,
        "one",
        delete_data=True,
        delete_trees=["install_dir"],
        applications_dir=tmp_path / "a",
        desktop_dir=tmp_path / "d",
    )

    assert not data.exists()
