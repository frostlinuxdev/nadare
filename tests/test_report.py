# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import os
from pathlib import Path

from nadare.core.report import _detect_virtualisation, build_report, redact


def test_redact_sensitive_identifiers_without_mangling_words() -> None:
    value = (
        r"/home/u Z:\home\u Musician email u@example.org "
        "tok=AbCdEf01234567890123456789012345 public 8.8.8.8 "
        "private 192.168.1.2 hostbox"
    )
    result = redact(value, username="u", home="/home/u", hostname="hostbox")
    assert "/home/u" not in result and r"Z:\home\u" not in result
    assert "~ ~" in result
    assert "Musician" in result
    assert "u@example.org" not in result
    assert "AbCdEf01234567890123456789012345" not in result
    assert "8.8.8.8" not in result and "192.168.1.2" in result
    assert "hostbox" not in result


def test_builder_degrades_missing_host_pieces_and_redacts_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("user@example.org 8.8.4.4\n", encoding="utf-8")
    report = build_report(
        {"active_profile": "unknown", "profiles": {}},
        logs_path=logs,
        profiles_path=tmp_path / "profiles",
        username="fakeuser",
        home=tmp_path / "home" / "fakeuser",
        hostname="fakehost",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert "Environment" in report and "Profile" in report and "Logs" in report
    assert "unknown" in report
    assert "user@example.org" not in report
    assert "8.8.4.4" not in report


def test_builder_caps_newest_log_content(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("old\n" * 100000 + "NEWEST\n", encoding="utf-8")
    report = build_report(
        {"profiles": {}, "active_profile": ""},
        logs_path=logs,
        profiles_path=tmp_path / "profiles",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        max_bytes=300_000,
    )
    assert len(report.encode()) <= 300_000
    assert "NEWEST" in report
    assert report.rfind("NEWEST") > report.find("### nadare.log")


def test_profile_settings_drop_secret_fields_and_shorten_paths(tmp_path: Path) -> None:
    report = build_report(
        {
            "profiles": {
                "demo": {
                    "backend": "stove-czn",
                    "title": "Demo",
                    "prefix": str(tmp_path / "prefix"),
                    "refresh_token": "danger",
                    "extra_env": {"TOKEN": "danger"},
                }
            },
            "active_profile": "demo",
        },
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="tester",
        home=tmp_path,
        hostname="host",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert "danger" not in report
    assert str(tmp_path) not in report


def test_cli_report_writes_private_file_and_stdout_mode(tmp_path, monkeypatch, capsys) -> None:
    from nadare import app

    for key in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"profiles": {}, "active_profile": ""}), encoding="utf-8")
    output = tmp_path / "report.txt"
    assert app.run_cli(["--config", str(config), "report", "-o", str(output)]) == 0
    assert output.exists()
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert capsys.readouterr().out.strip() == str(output)

    assert app.run_cli(["--config", str(config), "report", "--stdout"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("# Nadare support report")


def test_virtualisation_fallbacks_use_injected_proc_and_dmi(tmp_path) -> None:

    proc = tmp_path / "proc"
    proc.mkdir()
    sys_root = tmp_path / "sys"
    dmi = sys_root / "class/dmi/id"
    dmi.mkdir(parents=True)

    def detect(version: str, cpu: str = "", vendor: str = "") -> str:
        (proc / "version").write_text(version, encoding="utf-8")
        (proc / "cpuinfo").write_text(cpu, encoding="utf-8")
        (dmi / "sys_vendor").write_text(vendor, encoding="utf-8")
        return _detect_virtualisation(lambda *a, **k: "none", proc, sys_root)

    assert detect("Linux", "processor : 0") == "none"
    assert detect("Linux", "flags: fpu hypervisor kvm") == "kvm"
    assert detect("Linux", vendor="VMware, Inc.") == "vmware"
    assert detect("Linux version 6.1 Microsoft") == "wsl"
    assert _detect_virtualisation(lambda *a, **k: "qemu", proc, sys_root) == "qemu"


# ---------------------------------------------------------------- more redaction cases
def test_redaction_is_case_insensitive_and_only_for_whole_names() -> None:
    result = redact(
        r"/HOME/Alice/x z:\home\alice\y Alice Alicen malice alice_x",
        username="alice",
        home="/home/alice",
        hostname="",
    )
    assert "~/x" in result and "~\\y" in result
    assert "Alicen" in result and "malice" in result  # not mangled
    assert result.count("USER") == 2  # "Alice" and "alice_x" (underscore is not alphanumeric)


def test_public_ipv6_is_redacted_and_private_ranges_stay() -> None:
    result = redact(
        "a 2001:4860:4860::8888 b fe80::1 c [2606:4700::1111] d 10.0.0.5 e 127.0.0.1 f 1.1.1.1",
        username="",
        home="/nonexistent-home",
        hostname="",
    )
    assert "2001:4860:4860::8888" not in result and "2606:4700::1111" not in result
    assert "1.1.1.1" not in result
    assert "fe80::1" in result and "10.0.0.5" in result and "127.0.0.1" in result


def test_version_numbers_and_times_are_not_mistaken_for_addresses() -> None:
    text = "dwproton-11.0-15 at 12:34:56 build 1.2.3"
    assert redact(text, username="", home="/nonexistent-home", hostname="") == text


def test_the_report_stub_hash_matches_the_backend_pin() -> None:
    from nadare.backends.czn import backend
    from nadare.core import report

    assert report.STUB_PREBUILT_SHA256 == backend.STUB_PREBUILT_SHA256


def _czn_fixture(tmp_path: Path):
    home = tmp_path / "home" / "fakeuser"
    logs = tmp_path / "logs"
    profiles = tmp_path / "profiles"
    (profiles / "czn" / "stub").mkdir(parents=True)
    (profiles / "czn" / "stub" / "czn_pcsdk_stub.exe").write_bytes(b"not the shipped stub")
    logs.mkdir()
    (logs / "nadare.log").write_text(
        "login ok for fakeuser@example.org from 93.184.216.34 "
        "token=ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n"
        f"opening {home}/Games/czn\n",
        encoding="utf-8",
    )
    (logs / "czn-launch.log").write_text("launch line one\nlaunch line two\n", encoding="utf-8")
    (logs / "czn-czn-stub.log").write_text(
        "stub v1 Z:\\home\\fakeuser\\x\nsecond\n", encoding="utf-8"
    )
    (logs / "other-launch.log").write_bytes(b"\xff\xfe binary \x00 garbage")
    game = tmp_path / "game"
    (game / "bin").mkdir(parents=True)
    (game / "bin" / "ucldr_x_GL_loader.dll").write_bytes(b"x")
    (game / "crashpad_dumps").mkdir()
    config = {
        "active_profile": "czn",
        "profiles": {
            "czn": {
                "backend": "stove-czn",
                "title": "Chaos Zero Nightmare",
                "install_dir": str(game),
                "prefix": f"{home}/prefix",
                "runner": "dwproton",
                "refresh_token": "SECRETREFRESH",
                "extra_env": {"MY_KEY": "SECRETENVVALUE"},
                "cookie": "SECRETCOOKIE",
            }
        },
    }
    return config, logs, profiles, home


def test_full_czn_report_snapshot_leaks_nothing(tmp_path: Path) -> None:
    config, logs, profiles, home = _czn_fixture(tmp_path)
    report = build_report(
        config,
        profile_name="czn",
        logs_path=logs,
        profiles_path=profiles,
        username="fakeuser",
        home=home,
        hostname="fakehost",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        doctor_text="doctor ok on fakehost for fakeuser",
    )
    for secret in (
        "SECRETREFRESH",
        "SECRETENVVALUE",
        "SECRETCOOKIE",
        "fakeuser",
        "fakehost",
        "fakeuser@example.org",
        "93.184.216.34",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ):
        assert secret not in report, secret
    assert "CZN stub: czn_pcsdk_stub.exe sha256=" in report and "shipped=no" in report
    assert "CZN stub banner: stub v1" in report
    assert "### nadare.log" in report and "### czn-launch.log" in report
    assert "launch line two" in report
    assert "Game file: ucldr_x_GL_loader.dll" in report
    assert "Game dump folder: crashpad_dumps" in report
    assert "doctor ok on <host> for USER" in report


def test_a_shipped_stub_is_reported_as_shipped(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from nadare.core import report as report_mod

    config, logs, profiles, home = _czn_fixture(tmp_path)
    stub = profiles / "czn" / "stub" / "czn_pcsdk_stub.exe"
    monkeypatch.setattr(
        report_mod, "STUB_PREBUILT_SHA256", hashlib.sha256(stub.read_bytes()).hexdigest()
    )
    text = build_report(
        config,
        profile_name="czn",
        logs_path=logs,
        profiles_path=profiles,
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert "shipped=yes" in text


def test_binary_garbage_in_a_log_does_not_break_the_report(tmp_path: Path) -> None:
    config, logs, profiles, home = _czn_fixture(tmp_path)
    text = build_report(
        config,
        profile_name="other",
        logs_path=logs,
        profiles_path=profiles,
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert "profile other not found" in text


def test_a_non_czn_profile_has_no_stub_lines(tmp_path: Path) -> None:
    text = build_report(
        {"active_profile": "g", "profiles": {"g": {"backend": "direct-bd2", "title": "BD2"}}},
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert "CZN stub" not in text and "Backend: direct-bd2" in text


def test_truncation_keeps_the_newest_lines_and_stays_under_the_cap(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text(
        "".join(f"line {n} " + "x" * 2000 + "\n" for n in range(400)), encoding="utf-8"
    )
    text = build_report(
        {"profiles": {}, "active_profile": ""},
        logs_path=logs,
        profiles_path=tmp_path / "p",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )
    assert len(text.encode()) <= 300_000
    assert "line 399 " in text and "line 0 " not in text


def test_the_cli_exits_zero_when_pieces_are_missing(tmp_path, monkeypatch, capsys) -> None:
    from nadare import app

    for key in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"profiles": {}, "active_profile": ""}), encoding="utf-8")
    assert app.run_cli(["--config", str(config), "report", "-p", "nope", "--stdout"]) == 0
    assert "unavailable" in capsys.readouterr().out


def test_the_cli_reports_an_unwritable_target_and_exits_one(tmp_path, monkeypatch, capsys) -> None:
    from nadare import app

    for key in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"profiles": {}, "active_profile": ""}), encoding="utf-8")
    target = tmp_path / "ro"
    target.mkdir()
    assert app.run_cli(["--config", str(config), "report", "-o", str(target)]) == 1
    assert "cannot write report" in capsys.readouterr().err


# ---------------------------------------------------------------------- GUI
def test_report_dialog_copy_and_save(qapp, tmp_path, monkeypatch) -> None:
    from PyQt6.QtWidgets import QApplication, QFileDialog

    from nadare.gui.support_report import ReportDialog

    dialog = ReportDialog(None, "demo")
    assert dialog.text.isReadOnly()
    dialog.set_report("the exact text")
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == "the exact text"
    target = tmp_path / "out.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    dialog.save_button.click()
    assert target.read_text(encoding="utf-8") == "the exact text"
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_game_page_and_settings_page_offer_the_button(qapp) -> None:
    from nadare.gui.game_view import GameView
    from nadare.gui.settings import SettingsPage

    seen: list[str] = []
    view = GameView()
    view._profile = "demo"
    view.report_requested.connect(seen.append)
    view.report.click()
    page = SettingsPage()
    page._profile = "demo2"
    page.report_requested.connect(seen.append)
    page.report_button.click()
    assert seen == ["demo", "demo2"]
