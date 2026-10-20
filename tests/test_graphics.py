# SPDX-License-Identifier: GPL-3.0-or-later
"""Weak-GPU compatibility: Vulkan probe, renderer choice and diagnostics.

No Vulkan and no GPU are needed: the ``vulkaninfo`` path is fed fake text, the
``libvulkan`` fallback is monkeypatched, and process maps come from a fake
``/proc`` root.
"""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from nadare.core import graphics

# ---------------------------------------------------------------------------
# vulkaninfo parsing
# ---------------------------------------------------------------------------
_SUMMARY = """\
Vulkan Instance Version: 1.3.280

Devices:
========
GPU0:
\tapiVersion         = 1.3.280
\tdriverVersion      = 550.54.14.0
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA GeForce RTX 3060
\tdriverName         = NVIDIA

GPU1:
\tapiVersion         = 1.3.275
\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName         = Intel UHD Graphics 630
\tdriverName         = Intel
"""


def test_parse_summary_first_non_cpu_device() -> None:
    info = graphics.parse_vulkan_summary(_SUMMARY)
    assert info.available
    assert info.api_version == "1.3.280"
    assert info.device == "NVIDIA GeForce RTX 3060"
    assert info.driver == "NVIDIA"


def test_parse_summary_skips_cpu_llvmpipe() -> None:
    text = """\
Vulkan Instance Version: 1.3.275
GPU0:
\tapiVersion = 1.3.275
\tdeviceType = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName = llvmpipe (LLVM 17.0)
\tdriverName = llvmpipe
GPU1:
\tapiVersion = 1.2.170
\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName = AMD Radeon RX 580
\tdriverName = RADV
"""
    info = graphics.parse_vulkan_summary(text)
    assert info.device == "AMD Radeon RX 580"
    assert info.api_version == "1.2.170"


def test_parse_summary_only_cpu_is_unavailable() -> None:
    text = """\
GPU0:
\tapiVersion = 1.3.0
\tdeviceType = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName = llvmpipe
"""
    assert graphics.parse_vulkan_summary(text).available is False


def test_normalize_numeric_vulkan_version() -> None:
    # VK_MAKE_VERSION(1, 3, 280)
    value = (1 << 22) | (3 << 12) | 280
    assert graphics._normalize_version(value) == "1.3.280"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x40316d", "1.3.365"),  # older vulkaninfo prints the packed value in hex
        ("0X403000", "1.3.0"),
        ("0x403000 (1.3.0)", "1.3.0"),
        ("4206796 (1.3.204)", "1.3.204"),
        ("1.4.305 (conformance 1.4.0.0)", "1.4.305"),
    ],
)
def test_normalize_hex_and_annotated_vulkan_versions(raw: str, expected: str) -> None:
    assert graphics._normalize_version(raw) == expected


def test_hex_api_version_in_a_summary_is_not_read_as_old_vulkan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#74): ``apiVersion = 0x403000`` used to parse as (0, 403000)
    and silently demote the machine to OpenGL."""
    text = """
Devices:
========
GPU0:
    apiVersion         = 0x403000
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = Test GPU
    driverName         = nvidia
"""
    info = graphics.parse_vulkan_summary(text)
    assert info.api_version == "1.3.0"
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: info)
    assert graphics.dxvk_ok() is True


# ---------------------------------------------------------------------------
# vulkan_info / dxvk_ok
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    graphics.reset_cache()


def test_vulkan_info_uses_vulkaninfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics.shutil, "which", lambda name, path=None: "/usr/bin/vulkaninfo")
    monkeypatch.setattr(
        graphics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=_SUMMARY, stderr="", returncode=0),
    )
    info = graphics.vulkan_info()
    assert info.available
    assert info.device == "NVIDIA GeForce RTX 3060"
    assert graphics.dxvk_ok() is True


def test_missing_vulkaninfo_is_unknown_not_loader_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    monkeypatch.setattr(graphics, "_ICD_DIRS", ())  # no driver manifests: loader alone is not a GPU
    monkeypatch.setattr(
        graphics,
        "_instance_version",
        lambda: graphics.VulkanInfo(available=True, api_version="1.3.275"),
    )
    assert graphics._query_vulkan().available is False
    graphics.reset_cache()
    assert graphics.effective_renderer({"renderer": "auto"})[0] == "opengl"


def test_vulkaninfo_no_drivers_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: "/usr/bin/vulkaninfo")
    monkeypatch.setattr(
        graphics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="", stderr="Found no drivers!", returncode=1),
    )
    monkeypatch.setattr(
        graphics, "_instance_version", lambda: graphics.VulkanInfo(True, "1.3.0")
    )
    assert graphics._query_vulkan().available is False


def test_loader_only_probe_is_marked_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no vulkaninfo, a hardware ICD makes Nadare assume DXVK works (so a
    missing vulkan-tools never demotes a real GPU), but the version it reports is
    the loader's, not the device's: say so (#75)."""
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    monkeypatch.setattr(graphics, "_hardware_icd_present", lambda: True)
    monkeypatch.setattr(
        graphics, "_instance_version", lambda: graphics.VulkanInfo(True, "1.3.204")
    )
    info = graphics._query_vulkan()
    assert info.available is True
    assert info.api_version == "1.3.204"
    assert info.loader_only is True
    # the gate itself is unchanged: still optimistic, so no GPU is demoted
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: info)
    assert graphics.dxvk_ok() is True


def test_vulkaninfo_probe_is_not_marked_loader_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: "/usr/bin/vulkaninfo")
    text = """
Devices:
========
GPU0:
    apiVersion         = 1.3.280
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = Test GPU
"""
    monkeypatch.setattr(
        graphics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=text, stderr="", returncode=0),
    )
    assert graphics._query_vulkan().loader_only is False


def test_vulkan_info_unknown_when_nothing_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(graphics, "_instance_version", lambda: graphics.VulkanInfo(False))
    info = graphics.vulkan_info()
    assert info.available is False
    assert graphics.dxvk_ok() is False
    assert graphics.vulkan_note() != ""


def test_dxvk_ok_requires_vulkan_13(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graphics,
        "vulkan_info",
        lambda **k: graphics.VulkanInfo(True, api_version="1.2.199", device="old", driver="x"),
    )
    assert graphics.dxvk_ok() is False
    assert "OpenGL" in graphics.vulkan_note()


def test_vulkan_info_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_which(name: str, path: str | None = None) -> str | None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(graphics.shutil, "which", fake_which)
    monkeypatch.setattr(graphics, "_instance_version", lambda: graphics.VulkanInfo(False))
    graphics.vulkan_info()
    graphics.vulkan_info()
    assert calls["n"] == 1


def test_vulkaninfo_uses_a_pinned_search_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """L10: discovery must not consult the process ``PATH``."""
    seen: dict[str, object] = {}

    def fake_which(name: str, path: str | None = None) -> str:
        seen["name"] = name
        seen["path"] = path
        return "/usr/bin/vulkaninfo"

    monkeypatch.setattr(graphics.shutil, "which", fake_which)
    assert graphics._vulkaninfo_path() == "/usr/bin/vulkaninfo"
    assert seen["name"] == "vulkaninfo"
    assert seen["path"] == graphics._VULKANINFO_PATH
    assert "/usr/bin" in str(seen["path"])


def test_vulkan_info_probes_once_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """L11: the probe cache must be locked (one probe for many callers)."""
    calls = {"n": 0}

    def slow_query() -> graphics.VulkanInfo:
        calls["n"] += 1
        time.sleep(0.05)
        return graphics.VulkanInfo(True, api_version="1.3.0")

    monkeypatch.setattr(graphics, "_query_vulkan", slow_query)
    threads = [threading.Thread(target=graphics.vulkan_info) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# renderer choice -> launch env
# ---------------------------------------------------------------------------
def test_renderer_choice_defaults_and_clamps() -> None:
    assert graphics.renderer_choice({}) == "auto"
    assert graphics.renderer_choice({"renderer": "opengl"}) == "opengl"
    assert graphics.renderer_choice({"renderer": "WAT"}) == "auto"


def test_effective_renderer_auto_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    renderer, note = graphics.effective_renderer({})
    assert renderer == "opengl"
    assert note and "OpenGL" in note
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    assert graphics.effective_renderer({}) == ("vulkan", None)


def test_renderer_env_proton_opengl() -> None:
    assert graphics.renderer_env({"renderer": "opengl"}, kind="proton") == {
        "PROTON_USE_WINED3D": "1"
    }


def test_renderer_env_wine_opengl_builtin_overrides() -> None:
    env = {"WINEDLLOVERRIDES": "mscoree=n"}
    got = graphics.renderer_env({"renderer": "opengl"}, kind="wine", environ=env)
    assert got["WINEDLLOVERRIDES"] == "mscoree=n;d3d9,d3d10core,d3d11,dxgi=b"


def test_renderer_env_vulkan_and_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    assert graphics.renderer_env({"renderer": "vulkan"}, kind="proton") == {}
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    assert graphics.renderer_env({}, kind="wine") == {}


# ---------------------------------------------------------------------------
# renderer diagnostic
# ---------------------------------------------------------------------------
def test_classify_maps() -> None:
    assert graphics.classify_maps("/usr/lib/dxvk/d3d11.dll") == "DXVK on Vulkan"
    assert graphics.classify_maps("...d3d12core.dll...") == "VKD3D on Vulkan"
    assert graphics.classify_maps("...wined3d.dll...") == "WineD3D on OpenGL"
    assert graphics.classify_maps("/usr/lib/libGL.so.1") == "OpenGL"
    # DXVK copied into the prefix (seen with Proton-CachyOS): no "dxvk" in paths,
    # and NVIDIA's Vulkan ICD is libGLX_nvidia.
    prefix_dxvk = (
        "/pfx/drive_c/windows/system32/d3d11.dll\n/pfx/drive_c/windows/system32/dxgi.dll\n"
        "/usr/lib/libGLX_nvidia.so.615.78.08\n/usr/lib/libvulkan.so.1.4.321"
    )
    assert graphics.classify_maps(prefix_dxvk) == "DXVK on Vulkan"
    wined3d = prefix_dxvk + "\n/pfx/system32/wined3d.dll"
    assert graphics.classify_maps(wined3d) == "WineD3D on OpenGL"
    assert graphics.classify_maps("/usr/lib/libvulkan.so.1") == "Vulkan"
    assert graphics.classify_maps("nothing here") == "unknown"


def test_classify_maps_ignores_wines_own_vkd3d_libraries() -> None:
    """Wine's libvkd3d-shader/-utils are mapped into ordinary game processes;
    they must not be read as VKD3D-Proton (found on a live CZN launch)."""
    wine_vkd3d = (
        "/runner/files/lib/vkd3d/x86_64-windows/libvkd3d-1.dll\n"
        "/runner/files/lib/vkd3d/x86_64-windows/libvkd3d-shader-1.dll\n"
        "/pfx/drive_c/windows/system32/d3d11.dll\n/pfx/drive_c/windows/system32/dxgi.dll\n"
        "/runner/files/lib/wine/x86_64-windows/winevulkan.dll"
    )
    assert graphics.classify_maps(wine_vkd3d) == "DXVK on Vulkan"
    assert graphics.classify_maps("/pfx/system32/d3d12core.dll") == "VKD3D on Vulkan"
    assert graphics.classify_maps("/pfx/system32/d3d12.dll") == "VKD3D on Vulkan"


def test_classify_maps_does_not_guess_before_vulkan_is_opened() -> None:
    early = (
        "/pfx/drive_c/windows/system32/d3d11.dll\n/pfx/drive_c/windows/system32/dxgi.dll\n"
        "/runner/files/lib/wine/x86_64-windows/wined3d.dll\n/usr/lib/libGLX_nvidia.so.1"
    )
    assert graphics.classify_maps(early) == "unknown"


def _fake_proc(root: Path, pid: int, maps: str) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "maps").write_text(maps, encoding="utf-8")


def test_detect_renderer_reads_fake_proc(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _fake_proc(root, 10, "/usr/lib/dxvk/d3d11.dll")
    _fake_proc(root, 11, "/usr/lib/libvulkan.so.1")
    assert graphics.detect_renderer([10, 11], proc_root=root) == "DXVK on Vulkan"
    assert graphics.detect_renderer([99], proc_root=root) == "unknown"


def test_renderer_probe_reports(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _fake_proc(root, 42, "/usr/lib/wined3d.dll")
    seen: list[str] = []
    probe = graphics.RendererProbe(
        lambda: [42], delay=0, proc_root=root, on_result=seen.append
    ).start()
    probe.join(timeout=5)
    assert probe.result == "WineD3D on OpenGL"
    assert seen == ["WineD3D on OpenGL"]


def test_renderer_probe_waits_past_the_splash_screen(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _fake_proc(root, 42, "/usr/lib/libGLX_nvidia.so.0")  # Proton itself, game not up yet
    samples: list[int] = []

    def pids() -> list[int]:
        samples.append(1)
        if len(samples) == 3:  # the game has loaded DXVK by the third sample
            (root / "42" / "maps").write_text(
                "/pfx/system32/d3d11.dll\n/usr/lib/libvulkan.so.1", encoding="utf-8"
            )
        return [42]

    seen: list[str] = []
    probe = graphics.RendererProbe(
        pids, delay=0, interval=0.01, timeout=5, proc_root=root, on_result=seen.append
    ).start()
    probe.join(timeout=5)
    assert seen == ["DXVK on Vulkan"]
    assert len(samples) == 3


def test_renderer_probe_gives_up_with_last_label(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _fake_proc(root, 42, "/usr/lib/libGL.so.1")
    seen: list[str] = []
    probe = graphics.RendererProbe(
        lambda: [42], delay=0, interval=0.01, timeout=0.05, proc_root=root, on_result=seen.append
    ).start()
    probe.join(timeout=5)
    assert seen == ["OpenGL"]


def test_renderer_probe_does_not_report_after_stop(tmp_path: Path) -> None:
    """L11: a slow sample must not call ``on_result`` after ``stop()``."""
    root = tmp_path / "proc"
    _fake_proc(root, 42, "/usr/lib/wined3d.dll")
    started = threading.Event()

    def pids() -> list[int]:
        started.set()
        time.sleep(0.2)  # outlive the owner's join timeout
        return [42]

    seen: list[str] = []
    probe = graphics.RendererProbe(
        pids, delay=0, interval=0.01, timeout=5, proc_root=root, on_result=seen.append
    ).start()
    assert started.wait(2)
    probe.stop()
    probe.join(timeout=2)
    assert seen == []


def test_query_vulkan_trusts_cpu_only_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "GPU0:\n\tapiVersion = 1.3.0\n"
        "\tdeviceType = PHYSICAL_DEVICE_TYPE_CPU\n\tdeviceName = llvmpipe\n"
    )
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: "/usr/bin/vulkaninfo")
    monkeypatch.setattr(
        graphics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=text, stderr="", returncode=0),
    )
    monkeypatch.setattr(
        graphics, "_instance_version", lambda: graphics.VulkanInfo(True, "1.4.3")
    )
    info = graphics._query_vulkan()
    assert info.available is False
    assert info.driver == "software"
    assert graphics.effective_renderer({"renderer": "auto"})[0] == "opengl"


def test_missing_vulkaninfo_with_a_hardware_icd_still_counts_as_vulkan(tmp_path, monkeypatch):
    from nadare.core import graphics

    icd = tmp_path / "icd.d"
    icd.mkdir()
    (icd / "radeon_icd.x86_64.json").write_text("{}")
    monkeypatch.setattr(graphics, "_ICD_DIRS", (str(icd),))
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    monkeypatch.setattr(
        graphics,
        "_instance_version",
        lambda: graphics.VulkanInfo(available=True, api_version="1.3.0"),
    )
    info = graphics._query_vulkan()
    assert info.available and info.api_version == "1.3.0"


def test_missing_vulkaninfo_with_only_a_software_icd_is_unavailable(tmp_path, monkeypatch):
    from nadare.core import graphics

    icd = tmp_path / "icd.d"
    icd.mkdir()
    (icd / "lvp_icd.x86_64.json").write_text("{}")
    monkeypatch.setattr(graphics, "_ICD_DIRS", (str(icd),))
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    monkeypatch.setattr(
        graphics,
        "_instance_version",
        lambda: graphics.VulkanInfo(available=True, api_version="1.3.0"),
    )
    assert graphics._query_vulkan().available is False


def test_missing_vulkaninfo_and_no_icd_is_unavailable(tmp_path, monkeypatch):
    from nadare.core import graphics

    monkeypatch.setattr(graphics, "_ICD_DIRS", (str(tmp_path / "nope"),))
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    assert graphics._query_vulkan().available is False
