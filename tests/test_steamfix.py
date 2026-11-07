# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the pure Steam prefix fixer core (no Steam, Wine or network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import x64_dll

from nadare.core import steamfix
from nadare.core.backends import BackendError


def _library(root: Path, app_id: str = "5019180", *, pfx: bool = True) -> Path:
    steamapps = root / "Steam Library" / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / f"appmanifest_{app_id}.acf").write_text('"AppState"\n{\n}\n')
    if pfx:
        (steamapps / "compatdata" / app_id / "pfx" / "drive_c").mkdir(parents=True)
    return steamapps


def _proc_pid(proc: Path, pid: int, argv: list[str], environ: dict[str, str] | None = None) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    env = "".join(f"{k}={v}\0" for k, v in (environ or {}).items())
    (entry / "environ").write_text(env)


# ---- fix table ---------------------------------------------------------

_WEBVIEW2_KEY = (
    "[Software\\\\Wow6432Node\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\"
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}]"
)


def _register_webview2(prefix, version: str) -> None:
    """The registration a finished WebView2 install leaves in the prefix."""
    (prefix / "system.reg").write_text(
        f'WINE REGISTRY Version 2\n\n{_WEBVIEW2_KEY} 1\n"pv"="{version}"\n'
    )

def test_fix_table_has_epic_seven_and_gfl2():
    epic = steamfix.fix_for("5019180")
    assert epic is not None
    assert epic.game == "Epic Seven"
    assert epic.needs == (steamfix.KEY_WEBVIEW2,)
    assert epic.verified is True

    gfl2 = steamfix.fix_for(3347400)
    assert gfl2 is not None
    assert "Girls' Frontline 2" in gfl2.game
    assert gfl2.needs == (steamfix.KEY_WEBVIEW2,)
    assert gfl2.verified is False


def test_fix_table_lookup_is_exact_and_unknown_is_none():
    assert steamfix.fix_for("3887700") is None  # Girls' Frontline 1 is not in the table
    assert steamfix.fix_for("") is None
    assert steamfix.fix_for("05019180") is None  # no prefix matching


# ---- library / prefix discovery ---------------------------------------
def test_find_library_searches_every_library(tmp_path):
    first = tmp_path / "first" / "steamapps"
    first.mkdir(parents=True)
    second = _library(tmp_path / "second")
    found = steamfix.find_library("5019180", steamapps_dirs=[first, second])
    assert found == second


def test_default_library_discovery_reads_native_and_extra_libraries(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    native = home / ".local" / "share" / "Steam" / "steamapps"
    native.mkdir(parents=True)
    extra = tmp_path / "Extra Library"
    (extra / "steamapps").mkdir(parents=True)
    (native / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t\t'
        f'"{extra}"\n\t}}\n}}\n'
    )
    (extra / "steamapps" / "appmanifest_5019180.acf").write_text("")
    dirs = steamfix.steamapps_dirs()
    assert extra / "steamapps" in dirs
    # The default path (no injected dirs) finds the game in the extra library.
    assert steamfix.find_library("5019180") == extra / "steamapps"


def test_find_library_not_installed_message(tmp_path):
    empty = tmp_path / "empty" / "steamapps"
    empty.mkdir(parents=True)
    with pytest.raises(BackendError) as info:
        steamfix.find_library("5019180", steamapps_dirs=[empty])
    assert "not installed" in info.value.message.lower()
    assert "5019180" in info.value.message


def test_find_library_reports_unresolved_flatpak(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    flatpak = (
        tmp_path
        / "home"
        / ".var"
        / "app"
        / "com.valvesoftware.Steam"
        / ".local"
        / "share"
        / "Steam"
        / "steamapps"
    )
    # The Flatpak app directory exists but its library path was not created.
    (tmp_path / "home" / ".var" / "app" / "com.valvesoftware.Steam").mkdir(parents=True)
    with pytest.raises(BackendError) as info:
        steamfix.find_library("5019180", steamapps_dirs=[flatpak])
    assert "flatpak" in info.value.message.lower()


def test_prefix_missing_explains_run_once(tmp_path):
    steamapps = _library(tmp_path / "lib", pfx=False)
    with pytest.raises(BackendError) as info:
        steamfix.locate("5019180", steamapps_dirs=[steamapps])
    assert "run" in info.value.message.lower()
    assert "once" in info.value.message.lower()


def test_locate_handles_paths_with_spaces(tmp_path):
    steamapps = _library(tmp_path / "My Games Here")
    location = steamfix.locate("5019180", steamapps_dirs=[steamapps])
    assert location.library == steamapps
    assert location.prefix == steamapps / "compatdata" / "5019180" / "pfx"
    assert " " in str(location.prefix)


# ---- prerequisite detection -------------------------------------------
def test_check_prerequisites_webview2_and_vcredist(tmp_path):
    assert steamfix.KEY_WEBVIEW2 in steamfix.PREREQUISITE_KEYS
    assert steamfix.KEY_VCREDIST in steamfix.PREREQUISITE_KEYS

    prefix = tmp_path / "pfx"
    found = {p.key: p.present for p in steamfix.check_prerequisites(prefix)}
    assert found == {steamfix.KEY_WEBVIEW2: False, steamfix.KEY_VCREDIST: False}

    web = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0"
    web.mkdir(parents=True)
    (web / "msedgewebview2.exe").write_bytes(b"MZ")
    _register_webview2(prefix, "1.0")
    system32 = prefix / "drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / "vcruntime140.dll").write_bytes(x64_dll(b"real"))
    (system32 / "msvcp140.dll").write_bytes(x64_dll(b"real"))
    found = {p.key: p.present for p in steamfix.check_prerequisites(prefix)}
    assert found == {steamfix.KEY_WEBVIEW2: True, steamfix.KEY_VCREDIST: True}


def test_check_prerequisites_rejects_wine_builtin_vcredist(tmp_path):
    prefix = tmp_path / "pfx"
    system32 = prefix / "drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / "vcruntime140.dll").write_bytes(b"Wine builtin DLL")
    (system32 / "msvcp140.dll").write_bytes(b"Wine builtin DLL")
    found = {p.key: p.present for p in steamfix.check_prerequisites(prefix)}
    assert found[steamfix.KEY_VCREDIST] is False


# ---- running gates -----------------------------------------------------
def test_running_game_blocks_the_fix(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 111, ["steam", "-applaunch", "5019180"])
    hits = steamfix.running_blockers("5019180", tmp_path / "pfx", proc_root=proc)
    assert [h.what for h in hits] == ["game"]
    assert hits[0].pid == 111


def test_appid_token_also_counts_as_running(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 222, ["reaper", "SteamLaunch", "AppId=5019180"])
    assert steamfix.running_blockers("5019180", tmp_path / "pfx", proc_root=proc)


def test_other_app_and_other_prefix_do_not_block(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 333, ["steam", "-applaunch", "3347400"])
    _proc_pid(
        proc,
        444,
        ["wineserver"],
        {"WINEPREFIX": str(tmp_path / "pfx-other")},
    )
    assert steamfix.running_blockers("5019180", tmp_path / "pfx", proc_root=proc) == []


def test_wineserver_for_the_prefix_blocks(tmp_path):
    proc = tmp_path / "proc"
    prefix = tmp_path / "pfx"
    _proc_pid(proc, 555, ["wineserver"], {"WINEPREFIX": str(prefix)})
    hits = steamfix.running_blockers("5019180", prefix, proc_root=proc)
    assert [h.what for h in hits] == ["wineserver"]


def test_vanishing_proc_entries_are_ignored(tmp_path):
    proc = tmp_path / "proc"
    (proc / "666").mkdir(parents=True)  # no cmdline/environ: it vanished mid-read
    (proc / "not-a-pid").mkdir()
    assert steamfix.running_blockers("5019180", tmp_path / "pfx", proc_root=proc) == []
    assert steamfix.running_blockers("5019180", tmp_path / "pfx", proc_root=tmp_path / "nope") == []


def test_plan_fix_refuses_while_the_game_runs(tmp_path):
    steamapps = _library(tmp_path / "lib")
    proc = tmp_path / "proc"
    _proc_pid(proc, 777, ["steam", "-applaunch", "5019180"])
    with pytest.raises(BackendError) as info:
        steamfix.plan_fix("5019180", steamapps_dirs=[steamapps], proc_root=proc)
    assert "running" in info.value.message.lower()


def test_plan_fix_refuses_unverified_row(tmp_path):
    steamapps = _library(tmp_path / "lib", "3347400")
    with pytest.raises(BackendError) as info:
        steamfix.plan_fix("3347400", steamapps_dirs=[steamapps], proc_root=tmp_path / "proc")
    assert "not verified" in info.value.message.lower()


def test_plan_fix_refuses_unknown_app(tmp_path):
    with pytest.raises(BackendError) as info:
        steamfix.plan_fix("3887700", steamapps_dirs=[], proc_root=tmp_path / "proc")
    assert "no known fix" in info.value.message.lower()


# ---- backup ------------------------------------------------------------
def test_prefix_size_counts_nested_files(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "deep").mkdir(parents=True)
    (prefix / "a.bin").write_bytes(b"x" * 10)
    (prefix / "drive_c" / "deep" / "b.bin").write_bytes(b"y" * 5)
    assert steamfix.prefix_size(prefix) == 15


def test_plan_backup_picks_a_new_name_and_never_overwrites(tmp_path):
    compatdata = tmp_path / "compatdata"
    compatdata.mkdir()
    first = steamfix.plan_backup(compatdata, "5019180", "webview2")
    assert first.name == "5019180.bak-pre-webview2"
    first.mkdir()
    second = steamfix.plan_backup(compatdata, "5019180", "webview2")
    assert second.name == "5019180.bak-pre-webview2-2"
    second.mkdir()
    third = steamfix.plan_backup(compatdata, "5019180", "webview2")
    assert third.name == "5019180.bak-pre-webview2-3"


def test_make_backup_copies_everything_and_refuses_to_overwrite(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "file.txt").write_text("hello")
    dest = tmp_path / "compatdata" / "5019180.bak-pre-webview2"
    dest.parent.mkdir()

    backup = steamfix.make_backup(prefix, dest)
    assert backup.size_bytes > 0
    assert (dest / "drive_c" / "file.txt").read_text() == "hello"

    (dest / "keep.txt").write_text("mine")
    with pytest.raises(BackendError):
        steamfix.make_backup(prefix, dest)
    assert (dest / "keep.txt").read_text() == "mine"


# ---- plan / run --------------------------------------------------------
def _plan(tmp_path, *, proc_root: Path | None = None) -> steamfix.FixPlan:
    steamapps = _library(tmp_path / "lib")
    return steamfix.plan_fix(
        "5019180",
        steamapps_dirs=[steamapps],
        proc_root=proc_root or tmp_path / "proc",
    )


def test_plan_fix_has_backup_then_install(tmp_path):
    plan = _plan(tmp_path)
    assert plan.complete is False
    assert plan.backup.name == "5019180.bak-pre-webview2"
    assert [s.action for s in plan.steps] == ["backup", "install"]
    assert plan.steps[1].installer == steamfix.KEY_WEBVIEW2
    assert plan.missing == (steamfix.KEY_WEBVIEW2,)


def test_plan_fix_is_complete_when_prerequisite_is_present(tmp_path):
    steamapps = _library(tmp_path / "lib")
    prefix = steamapps / "compatdata" / "5019180" / "pfx"
    web = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0"
    web.mkdir(parents=True)
    (web / "msedgewebview2.exe").write_bytes(b"MZ")
    _register_webview2(prefix, "1.0")
    system32 = prefix / "drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / "vcruntime140.dll").write_bytes(b"MZ")
    (system32 / "msvcp140.dll").write_bytes(b"MZ")

    plan = steamfix.plan_fix(
        "5019180", steamapps_dirs=[steamapps], proc_root=tmp_path / "proc"
    )
    assert plan.complete is True
    assert plan.missing == ()
    assert plan.steps == ()


def test_run_fix_backs_up_then_installs(tmp_path):
    plan = _plan(tmp_path)
    calls: list[tuple[Path, str]] = []

    def installer(prefix: Path, key: str) -> int:
        calls.append((prefix, key))
        return 0

    result = steamfix.run_fix(plan, run_installer=installer)
    assert result.ok is True
    assert calls == [(plan.prefix, steamfix.KEY_WEBVIEW2)]
    assert result.backups == (plan.backup,)
    assert plan.backup.is_dir()
    assert "Epic Seven" in result.line
    assert "WebView2" in result.line


def test_run_fix_stops_on_the_first_failure(tmp_path):
    plan = _plan(tmp_path)
    calls: list[str] = []

    def installer(prefix: Path, key: str) -> int:
        calls.append(key)
        raise RuntimeError("installer exploded")

    result = steamfix.run_fix(plan, run_installer=installer)
    assert result.ok is False
    assert result.failed == steamfix.KEY_WEBVIEW2
    assert calls == [steamfix.KEY_WEBVIEW2]
    assert "failed" in result.line.lower()
    assert "installer exploded" in result.line


def test_run_fix_treats_nonzero_exit_as_failure(tmp_path):
    plan = _plan(tmp_path)
    result = steamfix.run_fix(plan, run_installer=lambda prefix, key: 1)
    assert result.ok is False
    assert result.installed == ()
    assert result.failed == steamfix.KEY_WEBVIEW2


def test_run_fix_with_nothing_missing_does_not_back_up(tmp_path):
    steamapps = _library(tmp_path / "lib")
    prefix = steamapps / "compatdata" / "5019180" / "pfx"
    web = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.0"
    web.mkdir(parents=True)
    (web / "msedgewebview2.exe").write_bytes(b"MZ")
    _register_webview2(prefix, "1.0")
    system32 = prefix / "drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / "vcruntime140.dll").write_bytes(b"MZ")
    (system32 / "msvcp140.dll").write_bytes(b"MZ")
    plan = steamfix.plan_fix(
        "5019180", steamapps_dirs=[steamapps], proc_root=tmp_path / "proc"
    )
    result = steamfix.run_fix(plan, run_installer=lambda prefix, key: pytest.fail("ran"))
    assert result.ok is True
    assert result.backups == ()
    assert not plan.backup.exists()


def test_a_game_started_by_its_uri_blocks_the_fix(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 800, ["steam", "steam://rungameid/5019180"])
    steamapps = _library(tmp_path / "lib")
    blockers = steamfix.running_blockers(
        "5019180", steamapps / "compatdata" / "5019180" / "pfx", proc_root=proc
    )
    assert blockers


def test_a_backup_is_refused_before_copying_when_the_disk_is_too_full(tmp_path, monkeypatch):
    prefix = tmp_path / "compatdata" / "5019180" / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / "drive_c" / "big").write_bytes(b"x" * 1000)
    asked: list[tuple[Path, int]] = []

    def too_full(path, needed, *args, **kwargs):
        asked.append((Path(path), needed))
        raise BackendError("Not enough disk space", code=4)

    monkeypatch.setattr(steamfix.diskspace, "check_space", too_full)
    dest = prefix.parent / "5019180.bak-pre-webview2"
    with pytest.raises(BackendError, match="disk space"):
        steamfix.make_backup(prefix, dest)
    assert asked == [(dest.parent, 1000)]
    assert not dest.exists()
    assert not list(prefix.parent.glob(".*.partial-*"))


# ---- a running game without a prefix ------------------------------------
def test_game_running_sees_the_game_by_its_app_id(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 4242, ["reaper", "SteamLaunch", "AppId=3524660", "--", "game.exe"])
    assert steamfix.game_running("3524660", proc_root=proc)
    assert not steamfix.game_running("5019180", proc_root=proc)


def test_game_running_ignores_a_wineserver_and_an_empty_proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    assert not steamfix.game_running("3524660", proc_root=proc)
    _proc_pid(proc, 4243, ["/usr/bin/wineserver"], {"WINEPREFIX": str(tmp_path / "pfx")})
    assert not steamfix.game_running("3524660", proc_root=proc)
