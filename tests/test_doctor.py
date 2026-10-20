# SPDX-License-Identifier: GPL-3.0-or-later
"""The ``nadare doctor`` command (fake probes; no GPU, runner or network)."""

from __future__ import annotations

from nadare.app import _disk_space_report, build_parser, run_cli
from nadare.core import graphics, paths, process, runners
from nadare.core.config import Config


def test_parser_has_doctor() -> None:
    args = build_parser().parse_args(["doctor"])
    assert args.command == "doctor"


def test_doctor_reports_unavailable_vulkan(monkeypatch, capsys) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)

    code = run_cli(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Vulkan unavailable" in out
    assert "DXVK needs Vulkan >= 1.3" in out
    assert "umu-run: /usr/bin/umu-run" in out
    assert "backends: " in out and "MISSING" not in out
    assert "runners: none found" in out


def test_disk_space_report_dedupes_mounts(monkeypatch, tmp_path) -> None:
    from nadare.core import diskspace

    monkeypatch.setattr(diskspace, "free_bytes", lambda _p: 319 * (1 << 30))
    mounts = {
        str(paths.profiles_dir() / "a"): "/mnt/games",
        "/games/b": "/mnt/games",
        "/games/c": "/home",
    }
    monkeypatch.setattr(diskspace, "mount_point", lambda p: mounts.get(str(p), "/"))

    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="wine", title="A")
    config.add_profile("b", backend="wine", title="B", install_dir="/games/b")
    config.add_profile("c", backend="wine", title="C", install_dir="/games/c")

    lines = _disk_space_report(config)
    assert sum("/mnt/games" in line for line in lines) == 1
    assert any("319 GiB free on /mnt/games" in line for line in lines)
    assert any("/home" in line for line in lines)


def test_doctor_lists_stale_runner(monkeypatch, capsys) -> None:
    fake = {
        "name": "Proton-CachyOS",
        "version": "cachyos-1",
        "path": "/tmp/Proton-CachyOS",
        "family": "cachyos",
        "latest_tag": "cachyos-1",
        "latest_published": "2025-01-01T00:00:00Z",
        "stale": True,
        "outdated": False,
    }
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(runners, "all_runners", lambda: ["/tmp/Proton-CachyOS"])
    monkeypatch.setattr(runners, "runner_info", lambda path, **k: dict(fake))
    monkeypatch.setattr(runners, "stale_suggestion", lambda info: "GE-Proton11-9 is newer")

    code = run_cli(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Proton-CachyOS" in out
    assert "STALE" in out
    assert "GE-Proton11-9 is newer" in out


def test_doctor_reports_missing_backends(monkeypatch, capsys) -> None:
    from nadare.core import backends

    monkeypatch.setattr(backends, "unavailable", lambda: ["arknights-yostar"])
    monkeypatch.setattr(backends, "kinds", lambda: ["arknights-yostar", "wine"])
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(runners, "all_runners", list)

    assert run_cli(["doctor"]) == 0
    assert "backends: 1/2 loaded, MISSING: arknights-yostar" in capsys.readouterr().out


def test_doctor_does_not_call_an_omitted_optional_backend_missing(monkeypatch, capsys) -> None:
    """A build made with NADARE_WITHOUT_FGO=1 leaves fgo-lepton out on purpose."""
    from nadare.core import backends

    monkeypatch.setattr(backends, "unavailable", lambda: ["fgo-lepton"])
    monkeypatch.setattr(backends, "kinds", lambda: ["wine", "arknights-yostar", "fgo-lepton"])
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(runners, "all_runners", list)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "MISSING" not in out
    assert "backends: 2/2 loaded" in out
    assert "not in this build (optional): fgo-lepton" in out


def test_doctor_still_reports_a_real_missing_backend_next_to_an_optional_one(
    monkeypatch, capsys
) -> None:
    from nadare.core import backends

    monkeypatch.setattr(backends, "unavailable", lambda: ["arknights-yostar", "fgo-lepton"])
    monkeypatch.setattr(backends, "kinds", lambda: ["wine", "arknights-yostar", "fgo-lepton"])
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(runners, "all_runners", list)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "backends: 1/2 loaded, MISSING: arknights-yostar" in out
    assert "not in this build (optional): fgo-lepton" in out


def test_doctor_says_when_the_vulkan_version_is_only_the_loader_s(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True, "1.3.204", loader_only=True)
    )
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(runners, "all_runners", list)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Vulkan loader 1.3.204" in out
    assert "device version not verified" in out
    assert "vulkan-tools" in out
