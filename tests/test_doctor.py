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


def _quiet_doctor(monkeypatch, which) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", which)
    monkeypatch.setattr(runners, "all_runners", list)


def test_doctor_names_a_system_7zip_and_mingw_when_present(monkeypatch, capsys) -> None:
    from nadare.core import sevenzip

    _quiet_doctor(monkeypatch, lambda name, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(sevenzip.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("CC", raising=False)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "7-Zip: system /usr/bin/7z" in out
    assert "mingw-w64: /usr/bin/x86_64-w64-mingw32-gcc" in out


def test_doctor_reports_the_bundled_7zip_without_copying_it(monkeypatch, capsys, tmp_path) -> None:
    from nadare.core import sevenzip

    _quiet_doctor(monkeypatch, lambda name, **k: None)
    monkeypatch.setattr(sevenzip.shutil, "which", lambda name: None)
    fake = tmp_path / "7zzs"
    fake.write_bytes(b"pretend 7-zip")
    monkeypatch.setattr(sevenzip, "BUNDLED", fake)
    monkeypatch.setattr(sevenzip, "BUNDLED_SHA256", sevenzip._digest(fake))
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert f"7-Zip: bundled {fake}" in out
    # a diagnostic must not install a copy of it
    assert not list((tmp_path / "cache").glob("bin/7zzs*"))


def test_doctor_explains_when_7zip_and_mingw_are_missing_but_stays_green(
    monkeypatch, capsys, tmp_path
) -> None:
    from nadare.core import sevenzip

    _quiet_doctor(monkeypatch, lambda name, **k: None)
    monkeypatch.setattr(sevenzip.shutil, "which", lambda name: None)
    monkeypatch.setattr(sevenzip, "BUNDLED", tmp_path / "absent")
    monkeypatch.delenv("CC", raising=False)

    assert run_cli(["doctor", "--strict"]) == 0  # optional tools never fail --strict
    out = capsys.readouterr().out
    assert "7-Zip: not found" in out and "Endfield" in out and "Neverness" in out
    assert "mingw-w64: not found" in out and "Endfield" in out
    assert "bundled launch helper" in out  # the shipped helper means it is not needed


def test_doctor_rejects_a_bundled_7zip_with_the_wrong_digest(monkeypatch, capsys, tmp_path) -> None:
    from nadare.core import sevenzip

    _quiet_doctor(monkeypatch, lambda name, **k: None)
    monkeypatch.setattr(sevenzip.shutil, "which", lambda name: None)
    fake = tmp_path / "7zzs"
    fake.write_bytes(b"tampered")
    monkeypatch.setattr(sevenzip, "BUNDLED", fake)
    run_cli(["doctor"])
    assert "7-Zip: not found" in capsys.readouterr().out


def test_doctor_honours_cc_for_the_mingw_line(monkeypatch, capsys) -> None:
    from nadare.core import sevenzip

    seen: list[str] = []

    def which(name, **k):
        seen.append(name)
        return f"/opt/{name}" if name == "my-mingw-gcc" else None

    _quiet_doctor(monkeypatch, which)
    monkeypatch.setattr(sevenzip.shutil, "which", lambda name: None)
    monkeypatch.setenv("CC", "my-mingw-gcc")
    run_cli(["doctor"])
    assert "mingw-w64: /opt/my-mingw-gcc" in capsys.readouterr().out


# ---------------------------------------------- a Secret Service that never answers (Fedora VM)
def test_keyring_diagnosis_gives_up_instead_of_hanging(monkeypatch) -> None:
    import threading
    import time

    from nadare.core import secrets

    release = threading.Event()
    monkeypatch.setattr(
        secrets, "_keyring_diagnosis_unbounded", lambda: release.wait(30) or (True, "x")
    )
    started = time.monotonic()
    ok, why = secrets.keyring_diagnosis(timeout=0.3)
    release.set()
    assert time.monotonic() - started < 5
    assert ok is False and "not responding" in why and "0.3" in why


def test_keyring_diagnosis_passes_a_normal_answer_through(monkeypatch) -> None:
    from nadare.core import secrets

    monkeypatch.setattr(secrets, "_keyring_diagnosis_unbounded", lambda: (True, "fine"))
    assert secrets.keyring_diagnosis(timeout=5) == (True, "fine")


def test_keyring_diagnosis_reports_a_crash_in_the_probe(monkeypatch) -> None:
    from nadare.core import secrets

    def boom():
        raise RuntimeError("dbus exploded")

    monkeypatch.setattr(secrets, "_keyring_diagnosis_unbounded", boom)
    ok, why = secrets.keyring_diagnosis(timeout=5)
    assert ok is False and "dbus exploded" in why


def test_doctor_does_not_wait_for_a_dead_keyring(monkeypatch, capsys) -> None:
    import threading
    import time

    from nadare.core import secrets

    release = threading.Event()
    monkeypatch.setattr(
        secrets, "_keyring_diagnosis_unbounded", lambda: release.wait(30) or (True, "x")
    )
    monkeypatch.setattr(secrets, "KEYRING_PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    started = time.monotonic()
    assert run_cli(["doctor"]) == 0
    release.set()
    assert time.monotonic() - started < 20
    assert "keyring: unavailable" in capsys.readouterr().out
