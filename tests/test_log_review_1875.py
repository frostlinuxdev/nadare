# SPDX-License-Identifier: GPL-3.0-or-later
"""The log system's release fixes from the #1875 review.

The support report keeps what a triager needs first (version, distro, runner) when it is cut, says
it runs as a Flatpak, and attaches only this game's flat logs; the game page's log zip has the
game's own logs; the redaction catches signed URLs, plain ``secret=`` pairs and bare JWTs; and the
log viewer shows a last line that has no newline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nadare.core import events, gamelogs, logview, report, runlog, sandbox
from nadare.core.config import Config


def build(tmp_path: Path, profile: dict | None = None, *, profiles: dict | None = None, **kw):
    config_path = tmp_path / "config.json"
    profs = profiles if profiles is not None else {"g": {"backend": "wine", "title": "Game"}}
    if profile is not None:
        profs = {"g": {"backend": "wine", "title": "Game", **profile}}
    config_path.write_text(json.dumps({"profiles": profs, "active_profile": next(iter(profs))}))
    (tmp_path / "os-release").write_text('PRETTY_NAME="Test Linux 1"\n')
    defaults = {
        "logs_path": tmp_path / "logs",
        "profiles_path": tmp_path / "profiles",
        "username": "synthetic-user",
        "home": tmp_path / "home",
        "hostname": "synthetic-host",
        "run_command": lambda *a, **k: "unavailable",
        "proc_root": tmp_path / "proc",
        "sys_root": tmp_path / "sys",
        "os_release_path": tmp_path / "os-release",
    }
    defaults.update(kw)
    return report.build_report(Config.load(config_path).data, **defaults)


def _run_log(logs: Path, game: str, operation: str, lines: list[str], mtime: float = 50) -> Path:
    folder = logs / runlog.game_id(game)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{operation}-20261127-0{int(mtime) % 10}0000.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


# ---- 1. the report keeps its first sections when it is cut ---------------------------------
def test_a_cut_report_keeps_environment_profile_and_runner(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("app noise line\n" * 5000, encoding="utf-8")
    for index, operation in enumerate(("install", "update", "verify", "launch")):
        _run_log(logs, "g", operation, [f"{operation} line {n}" for n in range(3000)], 10 + index)
    text = build(tmp_path, {}, max_bytes=8000)
    assert len(text.encode()) <= 8000
    for needed in ("Nadare:", "Distro: Test Linux 1", "Doctor:", "Profile: g", "Runner:"):
        assert needed in text, needed
    assert "truncated" in text
    assert "launch line 2999" in text  # the newest run keeps its newest lines
    assert "### nadare.log" in text and "### g/install-" in text  # every log keeps a part


def test_a_tiny_budget_still_respects_the_limit(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("noise\n" * 9000, encoding="utf-8")
    for limit in (64, 300, 1500):
        assert len(build(tmp_path, {}, max_bytes=limit).encode()) <= limit


# ---- 2. Flatpak: what it is and which distro it runs on --------------------------------------
def test_a_flatpak_report_says_flatpak_and_the_host_distro(tmp_path, monkeypatch):
    host = tmp_path / "host-os-release"
    host.write_text('PRETTY_NAME="Fedora Linux 43"\n')
    (tmp_path / "runtime-os-release").write_text('PRETTY_NAME="Freedesktop SDK 25.08"\n')
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(report, "HOST_OS_RELEASE", host)
    monkeypatch.setattr(report, "OS_RELEASE", tmp_path / "runtime-os-release")
    text = build(tmp_path, {}, os_release_path=None)
    assert "Run kind: Flatpak" in text
    assert "Distro: Fedora Linux 43" in text


def test_outside_flatpak_the_own_os_release_is_read(tmp_path, monkeypatch):
    (tmp_path / "own").write_text('PRETTY_NAME="Debian 13"\n')
    (tmp_path / "host").write_text('PRETTY_NAME="Wrong"\n')
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(report, "HOST_OS_RELEASE", tmp_path / "host")
    monkeypatch.setattr(report, "OS_RELEASE", tmp_path / "own")
    monkeypatch.delenv("APPIMAGE", raising=False)
    text = build(tmp_path, {}, os_release_path=None)
    assert "Distro: Debian 13" in text
    assert "Run kind: source/pip" in text


# ---- 3. the runner line says what a default runner is ---------------------------------------
def test_a_profile_without_a_runner_reports_the_default(tmp_path):
    text = build(tmp_path, {"backend": "stove-czn"})
    line = next(x for x in text.splitlines() if x.startswith("Runner:"))
    assert "unknown" not in line
    assert "default" in line


def test_a_named_runner_is_reported_as_it_is(tmp_path):
    text = build(tmp_path, {"runner": "GE-Proton10-25"})
    assert "Runner: GE-Proton10-25" in text


def test_a_custom_wine_binary_is_reported(tmp_path):
    text = build(tmp_path, {"backend": "wine", "wine_binary": "/opt/wine/bin/wine"})
    line = next(x for x in text.splitlines() if x.startswith("Runner:"))
    assert "/opt/wine/bin/wine" in line


# ---- 4. a cut run log keeps its exec/env lines ------------------------------------------------
def test_a_long_launch_log_keeps_the_env_line_from_its_head(tmp_path):
    logs = tmp_path / "logs"
    head = ["", "===== 2026-11-27T00:00:00 launch (g) | nadare 0.1 ====="]
    head += [f"setup step {n}" for n in range(15)]
    head += ["runner: DW-Proton", "exec: umu-run game.exe", "env: WINEDEBUG=-all"]
    _run_log(logs, "g", "launch", head + [f"spam {n}" for n in range(400)])
    text = build(tmp_path, {})
    assert "exec: umu-run game.exe" in text
    assert "env: " in text  # its value is masked, the line is there


# ---- 5. a flat log of another game with a longer name is not attached -----------------------
def test_a_flat_log_of_a_longer_named_game_is_not_attached(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "wuwa-launch.log").write_text("mine\n", encoding="utf-8")
    (logs / "wuwa-2-launch.log").write_text("someone else's\n", encoding="utf-8")
    profs = {"wuwa": {"backend": "wine"}, "wuwa-2": {"backend": "wine"}}
    text = build(tmp_path, profiles=profs)
    assert "### wuwa-launch.log" in text
    assert "wuwa-2-launch.log" not in text and "someone else's" not in text


# ---- 6. the username is hidden even when USER is not set ---------------------------------------
def test_the_username_is_redacted_without_user_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("LOGNAME", "alicefox")
    out = report.redact("logged in as alicefox", home=tmp_path / "x", hostname="h")
    assert "alicefox" not in out


def test_the_home_folder_name_is_the_last_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setattr(report, "_login_name", lambda: "")
    out = report.redact("user bobcat ran it", home="/home/bobcat", hostname="h")
    assert "bobcat" not in out


# ---- 7. redaction: signed URLs, plain secrets, bare JWTs --------------------------------------
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2"


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        ("GET https://cdn.example/f.zip?sig=SIGVALUE99&code=OAUTH123", ["SIGVALUE99", "OAUTH123"]),
        ("https://s3.example/o?X-Amz-Signature=abcdef0123&X-Amz-Credential=AKIA1", ["abcdef0123"]),
        ("redirect http://127.0.0.1:5/cb?state=STATE777&deviceKey=DEV888", ["STATE777", "DEV888"]),
        (
            "http://127.0.0.1/cb?redirectUrl=http%3A%2F%2Fh%2Fcb%3FaccessToken%3DNESTED555",
            ["NESTED555"],
        ),
        ("secret=mysecretvalue passwd=abc12345", ["mysecretvalue", "abc12345"]),
        (f"bearer token in log {JWT} end", [JWT]),
    ],
)
def test_known_secrets_are_masked(text, secrets):
    for masked in (events.mask_known(text), events.mask(text), logview.redacted(text)):
        for secret in secrets:
            assert secret not in masked, (masked, secret)


def test_a_url_stays_readable_after_masking():
    out = events.mask_known("GET https://cdn.example/game/f.zip?sig=SIGVALUE99&part=3")
    assert "https://cdn.example/game/f.zip?" in out
    assert "part=3" in out  # a harmless parameter stays


@pytest.mark.parametrize(
    "text",
    ["exit code=1 after 3 s", "state=installed", "keypass=ok bypass=1", "compass=north"],
)
def test_plain_words_outside_urls_are_left_alone(text):
    assert events.mask_known(text) == text


# ---- 8. game logs: the default prefix and Proton's pfx layout ---------------------------------
def _player_log(users: Path) -> Path:
    path = users / "steamuser" / "AppData" / "LocalLow" / "Co" / "Game" / "Player.log"
    path.parent.mkdir(parents=True)
    path.write_text("unity\n", encoding="utf-8")
    return path


def test_game_logs_are_found_in_the_default_prefix(tmp_path, monkeypatch):
    from nadare.core import paths

    monkeypatch.setattr(paths, "profiles_dir", lambda: tmp_path / "profiles")
    _player_log(tmp_path / "profiles" / "g" / "prefix" / "drive_c" / "users")
    install, prefix = gamelogs.folders({"backend": "wine"}, name="g")
    assert prefix == tmp_path / "profiles" / "g" / "prefix"
    assert any(log.path.name == "Player.log" for log in gamelogs.find(install, prefix))


def test_a_configured_prefix_still_wins(tmp_path):
    _install, prefix = gamelogs.folders({"prefix": str(tmp_path / "mine")}, name="g")
    assert prefix == tmp_path / "mine"


def test_a_missing_default_prefix_is_none(tmp_path, monkeypatch):
    from nadare.core import paths

    monkeypatch.setattr(paths, "profiles_dir", lambda: tmp_path / "profiles")
    assert gamelogs.folders({"backend": "script"}, name="g") == (None, None)


def test_game_logs_are_found_in_a_proton_pfx_folder(tmp_path):
    prefix = tmp_path / "compatdata" / "123"
    _player_log(prefix / "pfx" / "drive_c" / "users")
    assert any(log.path.name == "Player.log" for log in gamelogs.find(None, prefix))


def test_a_pfx_link_to_itself_is_not_listed_twice(tmp_path):
    prefix = tmp_path / "prefix"
    _player_log(prefix / "drive_c" / "users")
    (prefix / "pfx").symlink_to(".")
    found = [log for log in gamelogs.find(None, prefix) if log.path.name == "Player.log"]
    assert len(found) == 1


# ---- 9. the game page's Open logs zips the game's own logs ------------------------------------
@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_game_page_logs_include_the_game_folders(qapp, tmp_path):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        prefix = tmp_path / "pfx"
        view.set_profile(
            "g", {"title": "G", "install_dir": str(tmp_path / "i"), "prefix": str(prefix)}
        )
        view._open_logs()
        assert view._logs_dialog._game_folders == {"g": (tmp_path / "i", prefix)}
        view._logs_dialog.close()
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- 10. the log viewer ------------------------------------------------------------------------
def test_a_last_line_without_a_newline_is_shown(qapp, tmp_path):
    from nadare.gui.logs_dialog import LogsDialog

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("first\nfatal: died mid-line", encoding="utf-8")
    dlg = LogsDialog([], directory=logs, follow=False)
    assert "fatal: died mid-line" in dlg.view.toPlainText()
    assert "fatal: died mid-line" in dlg.current_text()


def test_following_a_finished_partial_line_shows_it_once(qapp, tmp_path):
    from nadare.gui.logs_dialog import LogsDialog

    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "nadare.log"
    log.write_text("first\nhalf", encoding="utf-8")
    dlg = LogsDialog([], directory=logs, follow=False)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(" done\nnext\n")
    os.utime(log, (100, 100))
    dlg._tick()
    shown = dlg.view.toPlainText()
    assert shown.count("half") == 1
    assert "half done\nnext" in shown


def test_a_saved_log_is_private(qapp, tmp_path, monkeypatch):
    from nadare.gui import logs_dialog
    from nadare.gui.logs_dialog import LogsDialog

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("line\n", encoding="utf-8")
    target = tmp_path / "saved.log"
    monkeypatch.setattr(
        logs_dialog.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")
    )
    old = os.umask(0o022)
    try:
        dlg = LogsDialog([], directory=logs, follow=False)
        dlg._save_as()
    finally:
        os.umask(old)
    assert target.read_text() == dlg.current_text()
    assert target.stat().st_mode & 0o777 == 0o600
