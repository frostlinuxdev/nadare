# SPDX-License-Identifier: GPL-3.0-or-later
"""Report a problem carries what helps fix a game, not a description of the whole machine.

Maintainer, 2026-11-11: "way too much info, mainly the system specs". The GPU line was the full
``lspci`` (every PCI device: chipset, USB, Wi-Fi, SSD models, network card); the report named every
other game with logs and when they ran; free space to the byte; the timezone; the AppImage path.
"""

from __future__ import annotations

from pathlib import Path

from nadare.core.report import build_report

LSPCI = "\n".join(
    [
        "00:00.0 Host bridge: Intel Corporation 12th Gen Core Processor Host Bridge (rev 05)",
        "00:02.0 Display controller: Intel Corporation Alder Lake-S GT1 [UHD Graphics 730]",
        "00:14.3 Network controller: Intel Corporation Raptor Lake-S PCH CNVi WiFi (rev 11)",
        "04:00.0 Non-Volatile memory controller: Biwin Storage Technology Co., Ltd. NVMe SSD",
        "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [Radeon RX 7900 XTX]",
        "06:00.0 Ethernet controller: Realtek Semiconductor Co., Ltd. RTL8125 2.5GbE Controller",
        "0a:00.0 3D controller: NVIDIA Corporation GA106M [GeForce RTX 3060 Mobile] (rev a1)",
    ]
)


def _runner(args, **_kw):
    return LSPCI if args[:1] == ["lspci"] else "unavailable"


def _report(tmp_path: Path, monkeypatch, **env) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    profile = "Blue Archive"
    config = {"active_profile": profile, "profiles": {profile: {"backend": "direct-bd2"}}}
    return build_report(
        config,
        profile_name=profile,
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="someone",
        home=tmp_path / "home",
        hostname="somehost",
        run_command=_runner,
        proc_root=tmp_path / "proc",
        doctor_text="doctor ok",
    )


def test_only_the_graphics_cards_are_listed(tmp_path, monkeypatch):
    report = _report(tmp_path, monkeypatch)
    for gpu in ("UHD Graphics 730", "Radeon RX 7900 XTX", "GeForce RTX 3060"):
        assert gpu in report
    for other in ("Host bridge", "WiFi", "NVMe SSD", "Ethernet", "RTL8125", "Biwin"):
        assert other not in report
    assert "03:00.0" not in report  # no bus addresses either


def test_other_games_are_counted_not_named(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    for game in ("genshin-impact", "nikke-goddess-of-victory"):
        (logs / game).mkdir(parents=True)
        (logs / game / "launch-20261105-120000.log").write_text("x", encoding="utf-8")
    (logs / "stray.log").write_text("x", encoding="utf-8")
    report = _report(tmp_path, monkeypatch)
    assert "genshin" not in report and "nikke" not in report and "stray.log" not in report
    assert "mtime=" not in report
    assert "2 other games" in report


def test_free_space_is_rounded(tmp_path, monkeypatch):
    report = _report(tmp_path, monkeypatch)
    free = [line for line in report.splitlines() if line.startswith("Free disk")]
    assert free and all("bytes" not in line for line in free)
    assert all("GiB" in line or "unavailable" in line for line in free)


def test_no_timezone_and_no_appimage_path(tmp_path, monkeypatch):
    report = _report(
        tmp_path,
        monkeypatch,
        TZ="Europe/London",
        APPIMAGE="/home/someone/Apps/My Nadare copy.AppImage",
        LANG="en_GB.UTF-8",
    )
    assert "Europe/London" not in report and "timezone:" not in report.lower()
    assert "My Nadare copy" not in report and "Run kind: AppImage" in report
    assert "en_GB.UTF-8" in report  # the language helps with text problems in games
