# SPDX-License-Identifier: GPL-3.0-or-later
"""Save logs takes each game's own logs, and Wine's errors reach the launch log (2026-11-26).

A user's game failed with only "exit code 5" in Nadare's logs. The cause (the install drive was
mounted noexec) was printed by Wine as an ``err:`` line, hidden by ``WINEDEBUG=-all``, and the
game's own logs sat in the prefix where the log zip never looked.
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

from nadare.core import gamelogs, logview, process

NOW = 1_800_000_000.0
DAY = 86400


def _write(path: Path, text: str = "line\n", age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _game(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "Game" / "install"
    prefix = tmp_path / "Game" / "prefix"
    install.mkdir(parents=True)
    (prefix / "drive_c" / "users" / "steamuser").mkdir(parents=True)
    return install, prefix


def _rel(found: list[gamelogs.GameLog]) -> set[str]:
    return {item.name for item in found}


# --- WINEDEBUG -----------------------------------------------------------------------------------


def test_wine_errors_are_kept_by_default() -> None:
    env = process.build_env({"prefix": "/games/pfx"}, base={})
    assert env["WINEDEBUG"] == "fixme-all"


def test_host_winedebug_still_wins() -> None:
    env = process.build_env({"prefix": "/games/pfx"}, base={"WINEDEBUG": "-all"})
    assert env["WINEDEBUG"] == "-all"


def test_profile_winedebug_still_wins() -> None:
    profile = {"prefix": "/games/pfx", "extra_env": {"WINEDEBUG": "+seh,+loaddll"}}
    env = process.build_env(profile, base={})
    assert env["WINEDEBUG"] == "+seh,+loaddll"


# --- finding a game's own logs -------------------------------------------------------------------


def test_finds_unity_unreal_proton_and_dxvk_logs(tmp_path: Path) -> None:
    install, prefix = _game(tmp_path)
    user = prefix / "drive_c" / "users" / "steamuser" / "AppData"
    _write(user / "LocalLow" / "kurogame" / "PGR" / "log" / "2026_11_26_07_09_32.log")
    _write(user / "LocalLow" / "Studio" / "Game" / "Player.log")
    _write(user / "Local" / "Client" / "Saved" / "Logs" / "Client.log")
    _write(install / "Client" / "Saved" / "Logs" / "Client.log")
    _write(prefix / "steam-default-dwproton-11.0-17.log")
    _write(prefix / "PGR_d3d11.log")
    _write(prefix / "PGR_dxgi.log")

    found = gamelogs.find(install, prefix, now=NOW)

    assert _rel(found) == {
        "prefix/AppData/LocalLow/kurogame/PGR/log/2026_11_26_07_09_32.log",
        "prefix/AppData/LocalLow/Studio/Game/Player.log",
        "prefix/AppData/Local/Client/Saved/Logs/Client.log",
        "install/Client/Saved/Logs/Client.log",
        "prefix/steam-default-dwproton-11.0-17.log",
        "prefix/PGR_d3d11.log",
        "prefix/PGR_dxgi.log",
    }


def test_old_logs_and_other_files_are_left_out(tmp_path: Path) -> None:
    install, prefix = _game(tmp_path)
    low = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow" / "S" / "G"
    _write(low / "Player.log", age_days=30)
    _write(low / "save.dat")
    _write(low / "notes.txt")
    _write(prefix / "drive_c" / "windows" / "setupapi.log")
    _write(prefix / "system.reg")

    assert gamelogs.find(install, prefix, now=NOW) == []


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    install, prefix = _game(tmp_path)
    outside = _write(tmp_path / "secret" / "private.log")
    low = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow"
    low.mkdir(parents=True)
    (low / "Link").symlink_to(outside.parent, target_is_directory=True)
    (prefix / "linked.log").symlink_to(outside)

    assert gamelogs.find(install, prefix, now=NOW) == []


def test_newest_logs_win_when_there_are_too_many(tmp_path: Path) -> None:
    install, prefix = _game(tmp_path)
    appdata = prefix / "drive_c" / "users" / "steamuser" / "AppData"
    log_dir = appdata / "LocalLow" / "K" / "G" / "log"
    for number in range(20):
        _write(log_dir / f"run{number:02d}.log", age_days=(20 - number) / 10)

    found = gamelogs.find(install, prefix, now=NOW, max_files=5)

    assert [item.path.name for item in found] == [f"run{n}.log" for n in (19, 18, 17, 16, 15)]


def test_a_huge_install_is_not_walked_whole(tmp_path: Path) -> None:
    install, prefix = _game(tmp_path)
    for number in range(50):
        (install / f"Data{number}" / "a" / "b").mkdir(parents=True)
    _write(install / "Data49" / "Saved" / "Logs" / "late.log")

    started = time.monotonic()
    found = gamelogs.find(install, prefix, now=NOW, max_entries=40)
    assert time.monotonic() - started < 5
    assert len(found) <= 1  # stopped early or found it; never an unbounded walk


def test_missing_folders_find_nothing(tmp_path: Path) -> None:
    assert gamelogs.find(tmp_path / "nope", None, now=NOW) == []
    assert gamelogs.find(None, None, now=NOW) == []


def test_folders_come_from_the_profile() -> None:
    profile = {"install_dir": "/g/install", "extra": {"prefix": "/g/prefix"}}
    assert gamelogs.folders(profile) == (Path("/g/install"), Path("/g/prefix"))
    assert gamelogs.folders({}) == (None, None)


# --- the zip -------------------------------------------------------------------------------------


def test_bundle_holds_the_game_logs_redacted(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _write(logs / "pgr" / "launch-20261126-070900.log", "launch\n")
    install, prefix = _game(tmp_path)
    home = str(Path.home())
    _write(
        prefix / "steam-default-dwproton-11.0-17.log",
        f"err:virtual:map_image_into_view noexec filesystem? {home}/x\n",
        age_days=0,
    )
    os.utime(prefix / "steam-default-dwproton-11.0-17.log")  # now, for the real clock

    dest = tmp_path / "out.zip"
    count = logview.bundle(
        ["pgr"], logs, dest, profile="pgr", game_folders={"pgr": (install, prefix)}
    )

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert "pgr/game/prefix/steam-default-dwproton-11.0-17.log" in names
        text = zf.read("pgr/game/prefix/steam-default-dwproton-11.0-17.log").decode()
        readme = zf.read("README.txt").decode()
    assert count == 2
    assert "noexec filesystem?" in text
    assert home not in text
    assert "1 game log" in readme


def test_bundle_for_one_game_skips_the_others(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    a_install, a_prefix = _game(tmp_path / "a")
    b_install, b_prefix = _game(tmp_path / "b")
    os.utime(_write(a_prefix / "steam-a.log"))
    os.utime(_write(b_prefix / "steam-b.log"))

    dest = tmp_path / "out.zip"
    logview.bundle(
        ["a", "b"],
        logs,
        dest,
        profile="a",
        game_folders={"a": (a_install, a_prefix), "b": (b_install, b_prefix)},
    )

    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
    assert "a/game/prefix/steam-a.log" in names
    assert not any("steam-b" in name for name in names)


def test_bundle_without_game_folders_is_unchanged(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _write(logs / "pgr" / "launch-20261126-070900.log")
    dest = tmp_path / "out.zip"
    assert logview.bundle(["pgr"], logs, dest, profile="pgr") == 1
    with zipfile.ZipFile(dest) as zf:
        assert not any("/game/" in name for name in zf.namelist())


# --- the Logs dialog -----------------------------------------------------------------------------


def test_logs_dialog_save_zip_passes_the_game_folders(qtbot, tmp_path, monkeypatch) -> None:
    from nadare.gui import logs_dialog as module

    logs = tmp_path / "logs"
    logs.mkdir()
    folders = {"pgr": (tmp_path / "install", tmp_path / "prefix")}
    dlg = module.LogsDialog(
        ["pgr"], profile="pgr", directory=logs, follow=False, game_folders=folders
    )
    qtbot.addWidget(dlg)
    seen = {}
    monkeypatch.setattr(
        module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "o.zip"), "")
    )
    monkeypatch.setattr(module.logview, "bundle", lambda *a, **k: seen.update(k) or 0)

    dlg._save_zip()

    assert seen["game_folders"] == folders


def test_main_window_folders_for_every_profile() -> None:
    from nadare.gui.main_window import game_log_folders

    profiles = {
        "pgr": {"install_dir": "/g/pgr/install", "prefix": "/g/pgr/prefix"},
        "bare": {},
    }
    assert game_log_folders(profiles) == {
        "pgr": (Path("/g/pgr/install"), Path("/g/pgr/prefix")),
        "bare": (None, None),
    }


def test_game_logs_hide_account_ids_and_jwts() -> None:
    text = (
        'last login cuid: 4432112 uid=99887766 {"accountId":"A1728x","userName":"frost"}\n'
        "openid: ab12cd34ef deviceId=XYZ-123\n"
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ end\n"
        "err:virtual noexec filesystem? Client.log Player.log uidesc: kept\n"
    )
    out = gamelogs.mask(text)
    for secret in ("4432112", "99887766", "A1728x", "frost", "ab12cd34ef", "XYZ-123", "eyJhbGci"):
        assert secret not in out, secret
    assert "noexec filesystem?" in out
    assert "uidesc: kept" in out


def test_game_logs_hide_prefixed_ids() -> None:
    text = (
        "hgid: 3141592653 machineguid=1a2b3c4d-0000 deviceUniqueIdentifier: d34db33f00\n"
        '{"distinct_id":"u-778899","roleId":12345678}\n'
        "GWLP_HWNDPARENT=0000 gameHandle: 6f0012 Id: 7\n"
    )
    out = gamelogs.mask(text)
    for secret in ("3141592653", "1a2b3c4d", "d34db33f00", "u-778899", "12345678"):
        assert secret not in out, secret
    assert "GWLP_HWNDPARENT=0000" in out
    assert "gameHandle: 6f0012" in out
