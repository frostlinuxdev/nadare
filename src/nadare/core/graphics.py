# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Weak-GPU compatibility: renderer choice, Vulkan probe and renderer detection.

DXVK/VKD3D 2.x need Vulkan 1.3. On old/weak GPUs that only expose Vulkan 1.0
or no Vulkan at all, a DXVK game shows a black screen or crashes. The escape
hatch is Wine's OpenGL-based Direct3D (WineD3D): Proton turns it on with
``PROTON_USE_WINED3D=1``, bare Wine with builtin ``d3d9/d3d10core/d3d11/dxgi``
DLL overrides.

Everything that touches those choices lives here so the Wine/Proton backends
share one implementation: the ``renderer`` profile option, the Vulkan capability
probe, the launch-environment mapping and the after-launch diagnostic that
reads ``/proc/<pid>/maps`` to see which renderer a running game actually uses.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Renderer profile values, in Settings order.
RENDERER_CHOICES: tuple[tuple[str, str], ...] = (
    ("Auto (Vulkan if available)", "auto"),
    ("Vulkan (DXVK)", "vulkan"),
    ("OpenGL (WineD3D, old GPUs)", "opengl"),
)

RENDERER_HELP = (
    "Graphics backend. Auto picks Vulkan when the machine supports it and falls "
    "back to OpenGL (WineD3D) otherwise. OpenGL is slower but works on old GPUs "
    "without Vulkan 1.3."
)

#: DLLs DXVK/VKD3D replace and the Wine builtin override that forces WineD3D.
_WINED3D_OVERRIDE = "d3d9,d3d10core,d3d11,dxgi=b"

#: Vulkan version DXVK/VKD3D 2.x require to work at all.
_MIN_DXVK_VERSION = (1, 3)

#: A runner family is stale when upstream has not released for this long.
STALE_DAYS = 90


@dataclass(frozen=True)
class VulkanInfo:
    """Result of the machine's Vulkan capability probe."""

    available: bool
    api_version: str | None = None
    device: str | None = None
    driver: str | None = None
    #: ``api_version`` is the loader's instance version, not a device's (no
    #: ``vulkaninfo``): a hardware ICD is present, but no GPU was enumerated.
    loader_only: bool = False


# ---------------------------------------------------------------------------
# Vulkan probe
# ---------------------------------------------------------------------------
_INFO_CACHE: dict[str, VulkanInfo] = {}
_INFO_LOCK = threading.Lock()


def _normalize_version(raw: Any) -> str | None:
    """Return ``major.minor.patch`` from a textual or numeric Vulkan version."""
    if raw in (None, ""):
        return None
    text = str(raw).strip().split()[0]
    value: int | None = None
    if re.fullmatch(r"\d+", text):
        value = int(text)
    elif re.fullmatch(r"0[xX][0-9a-fA-F]+", text):  # older vulkaninfo prints hex
        value = int(text, 16)
    if value is not None:
        return f"{value >> 22}.{(value >> 12) & 0x3FF}.{value & 0xFFF}"
    return text


_DEVICE_RE = re.compile(r"^\s*GPU\d+\s*:\s*$", re.IGNORECASE)
_FIELD_RE = re.compile(r"^\s*([A-Za-z]+)\s*=\s*(.+?)\s*$")
_INSTANCE_RE = re.compile(r"Vulkan Instance Version:\s*(\S+)")


def _devices(text: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if _DEVICE_RE.match(line):
            if current is not None:
                devices.append(current)
            current = {}
            continue
        if current is None:
            continue
        match = _FIELD_RE.match(line)
        if match:
            current[match.group(1).lower()] = match.group(2).strip()
    if current is not None:
        devices.append(current)
    return devices


def parse_vulkan_summary(text: str) -> VulkanInfo:
    """Parse ``vulkaninfo --summary`` into a :class:`VulkanInfo`.

    Only physical devices are considered, and the first non-CPU device wins
    (a machine may expose ``llvmpipe`` alongside a real GPU). The instance
    version is used when a device block omits ``apiVersion``.
    """
    instance_match = _INSTANCE_RE.search(text)
    instance_version = _normalize_version(instance_match.group(1)) if instance_match else None
    for device in _devices(text):
        kind = device.get("devicetype", "").upper()
        if kind.endswith("PHYSICAL_DEVICE_TYPE_CPU"):
            continue
        return VulkanInfo(
            available=True,
            api_version=_normalize_version(device.get("apiversion")) or instance_version,
            device=device.get("devicename") or None,
            driver=device.get("drivername") or None,
        )
    return VulkanInfo(available=False)


def _instance_version() -> VulkanInfo:
    """Probe ``libvulkan.so.1`` directly when ``vulkaninfo`` is absent."""
    try:
        lib = ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return VulkanInfo(available=False)
    try:
        fn = lib.vkEnumerateInstanceVersion
    except AttributeError:
        return VulkanInfo(available=False)
    version = ctypes.c_uint32()
    fn.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    fn.restype = ctypes.c_int
    try:
        if fn(ctypes.byref(version)) != 0:  # VK_SUCCESS
            return VulkanInfo(available=False)
    except OSError:
        return VulkanInfo(available=False)
    return VulkanInfo(available=True, api_version=_normalize_version(version.value))


def vulkan_info(*, refresh: bool = False) -> VulkanInfo:
    """Return (and cache) the machine's Vulkan capability.

    Prefers ``vulkaninfo --summary`` (device + driver names); falls back to
    loading ``libvulkan.so.1``; returns ``available=False`` when neither works.
    Cached for the life of the process because it shells out.  The cache is
    guarded so concurrent UI/doctor callers probe exactly once.
    """
    with _INFO_LOCK:
        if not refresh and "info" in _INFO_CACHE:
            return _INFO_CACHE["info"]
        info = _query_vulkan()
        _INFO_CACHE["info"] = info
        return info


#: Trusted directories searched for ``vulkaninfo`` (L10). The process ``PATH``
#: is deliberately not consulted, so a dropped ``vulkaninfo`` earlier on an
#: attacker-influenced ``PATH``/cwd cannot be executed.
_VULKANINFO_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _vulkaninfo_path() -> str | None:
    return shutil.which("vulkaninfo", path=_VULKANINFO_PATH)


#: Directories holding Vulkan ICD manifests (one JSON per installed driver).
_ICD_DIRS = (
    "/usr/share/vulkan/icd.d",
    "/usr/local/share/vulkan/icd.d",
    "/etc/vulkan/icd.d",
)
#: ICD manifests that are CPU rasterisers, not a GPU.
_SOFTWARE_ICD_MARKERS = ("lvp", "lavapipe", "swrast", "llvmpipe", "cpu")


def _hardware_icd_present(dirs: tuple[str, ...] | None = None) -> bool:
    """True when some installed Vulkan ICD manifest is not a software rasteriser."""
    import os

    for directory in dirs if dirs is not None else _ICD_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            lowered = name.lower()
            if not lowered.endswith(".json"):
                continue
            if any(marker in lowered for marker in _SOFTWARE_ICD_MARKERS):
                continue
            return True
    return False


def _query_vulkan() -> VulkanInfo:
    exe = _vulkaninfo_path()
    if exe:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [exe, "--summary"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            info = parse_vulkan_summary(text)
            if info.available:
                return info
            if "deviceType" in text:  # devices listed, all CPU/software
                return VulkanInfo(available=False, driver="software")
            if proc.returncode != 0:  # "Found no drivers": a loader alone is not a GPU
                return VulkanInfo(available=False)
        except (OSError, subprocess.SubprocessError):
            pass
    # No usable vulkaninfo. The loader's instance version alone says nothing about
    # a device, but an installed *hardware* ICD does: ``vulkan-tools`` is not
    # installed by default on most distros, so a missing vulkaninfo must not demote
    # a machine with a real GPU driver to WineD3D.
    if _hardware_icd_present():
        loader = _instance_version()
        if loader.available:
            return VulkanInfo(
                available=True, api_version=loader.api_version, loader_only=True
            )
    return VulkanInfo(available=False)


def has_display(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when an X11 or Wayland display is configured (#437).

    Windows installers such as WebView2 need one; with neither variable set
    (a headless ssh or tty session) they hang instead of failing.
    """
    env = os.environ if environ is None else environ
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def reset_cache() -> None:
    """Drop the cached Vulkan probe (tests, or a GPU hotplug)."""
    with _INFO_LOCK:
        _INFO_CACHE.clear()


def dxvk_ok() -> bool:
    """True when Vulkan is present and new enough for DXVK/VKD3D 2.x."""
    info = vulkan_info()
    if not info.available or not info.api_version:
        return False
    parts = _normalize_version(info.api_version)
    if not parts:
        return False
    nums = tuple(int(p) for p in re.findall(r"\d+", parts)[:3])
    return nums[:2] >= _MIN_DXVK_VERSION


def vulkan_note() -> str:
    """One-line warning for game status when DXVK cannot work here."""
    if dxvk_ok():
        return ""
    return (
        "Your graphics driver looks old for Vulkan games. If the game will not "
        "start, open Settings and try the OpenGL renderer."
    )


# ---------------------------------------------------------------------------
# renderer choice -> launch environment
# ---------------------------------------------------------------------------
def renderer_choice(profile: Mapping[str, Any] | None) -> str:
    """The profile's renderer setting, clamped to a known value."""
    if not profile:
        return "auto"
    value = str(profile.get("renderer") or "auto").strip().lower()
    return value if value in ("auto", "vulkan", "opengl") else "auto"


def effective_renderer(profile: Mapping[str, Any] | None) -> tuple[str, str | None]:
    """Resolve ``auto`` to ``vulkan``/``opengl`` and explain a fallback.

    Returns ``(renderer, note)`` where the note is non-``None`` only when
    ``auto`` had to fall back to OpenGL.
    """
    choice = renderer_choice(profile)
    if choice == "auto":
        if dxvk_ok():
            return "vulkan", None
        return "opengl", "Vulkan < 1.3; falling back to OpenGL (WineD3D) for this launch"
    return choice, None


def _merge_overrides(existing: str, wanted: str) -> str:
    """Append ``wanted`` DLL overrides without discarding the user's own."""
    parts = [p.strip() for p in existing.split(";") if p.strip()]
    parts.append(wanted)
    return ";".join(parts)


def renderer_env(
    profile: Mapping[str, Any] | None,
    *,
    kind: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment additions that select the profile's renderer.

    ``kind`` is ``"proton"`` for umu/Proton launches and ``"wine"`` for bare
    Wine. Vulkan adds nothing (DXVK is the default); OpenGL sets
    ``PROTON_USE_WINED3D=1`` for Proton or builtin Direct3D DLL overrides for
    Wine. Callers merge the result into the child environment.
    """
    renderer, _note = effective_renderer(profile)
    if renderer == "vulkan":
        return {}
    if kind == "proton":
        return {"PROTON_USE_WINED3D": "1"}
    existing = ""
    if environ is not None:
        existing = str(environ.get("WINEDLLOVERRIDES") or "")
    return {"WINEDLLOVERRIDES": _merge_overrides(existing, _WINED3D_OVERRIDE)}


# ---------------------------------------------------------------------------
# after-launch renderer diagnostic (/proc/<pid>/maps)
# ---------------------------------------------------------------------------
def classify_maps(text: str) -> str:
    """Classify one or more ``/proc/<pid>/maps`` blobs into a renderer label."""
    low = text.lower()
    if "dxvk" in low:
        return "DXVK on Vulkan"
    # Only an actual D3D12 module means VKD3D-Proton. Wine's own
    # ``libvkd3d-shader``/``libvkd3d-utils`` are mapped into most game
    # processes (HLSL compilation) and say nothing about the renderer.
    if "d3d12core" in low or "/d3d12.dll" in low or "vkd3d-proton" in low:
        return "VKD3D on Vulkan"
    # Proton often copies DXVK into the prefix as plain system32/d3d11.dll etc.,
    # so no "dxvk" in the path. A D3D dll plus the Vulkan loader is DXVK.
    # (libGLX_* is also NVIDIA's Vulkan ICD, so the GL check comes last.)
    has_d3d = any(f"/{dll}.dll" in low for dll in ("d3d11", "dxgi", "d3d10core", "d3d9"))
    vulkan = "libvulkan" in low or "winevulkan" in low or "vulkan-1" in low
    if "wined3d" in low and (not has_d3d or "libvulkan" in low):
        return "WineD3D on OpenGL"
    if has_d3d and vulkan:
        return "DXVK on Vulkan"
    if has_d3d and "wined3d" in low:
        # D3D dlls and wined3d mapped but nothing has opened Vulkan yet: the
        # game is still starting (launcher/shield stage). Don't guess.
        return "unknown"
    if "libgl" in low or "libegl" in low or "libglx" in low:
        return "OpenGL"
    if "libvulkan" in low:
        return "Vulkan"
    return "unknown"


def read_maps(pid: int, *, proc_root: str | Path = "/proc") -> str:
    try:
        return (Path(proc_root) / str(pid) / "maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_renderer(pids: Iterable[int], *, proc_root: str | Path = "/proc") -> str:
    """Scan the maps of ``pids`` and report the renderer actually in use."""
    joined = "\n".join(read_maps(pid, proc_root=proc_root) for pid in pids)
    return classify_maps(joined)


class RendererProbe:
    """Record the renderer a running game settles on, off the launch thread.

    A daemon thread waits ``delay`` seconds, then scans the pids returned by
    ``pids`` every ``interval`` seconds until a Direct3D translation layer shows
    up (DXVK/VKD3D/WineD3D), the probe is stopped (the game exited) or
    ``timeout`` seconds have passed, and stores the last label. A single early
    sample is not enough: Arknights sits in its CEF launcher for minutes before
    the game loads D3D, and until then only Proton's own GL/Vulkan libs show. The result is readable
    from :attr:`result` and reported to ``on_result`` once.
    """

    FINAL = frozenset({"DXVK on Vulkan", "VKD3D on Vulkan", "WineD3D on OpenGL"})

    def __init__(
        self,
        pids: Callable[[], Iterable[int]],
        *,
        delay: float = 20.0,
        interval: float = 5.0,
        timeout: float = 600.0,
        on_result: Callable[[str], None] | None = None,
        proc_root: str | Path = "/proc",
    ) -> None:
        self._pids = pids
        self.delay = max(0.0, float(delay))
        self.interval = max(0.01, float(interval))
        self.timeout = max(self.delay, float(timeout))
        self.on_result = on_result
        self.proc_root = proc_root
        self.result: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> RendererProbe:
        self._thread = threading.Thread(target=self._run, name="nadare-renderer-probe", daemon=True)
        self._thread.start()
        return self

    def _sample(self) -> str:
        try:
            pids = list(self._pids())
        except Exception:  # noqa: BLE001 - a probe must never break a launch
            pids = []
        return detect_renderer(pids, proc_root=self.proc_root)

    def _run(self) -> None:
        if self._stop.wait(self.delay):
            return
        waited = self.delay
        label = self._sample()
        while label not in self.FINAL and waited < self.timeout:
            if self._stop.wait(self.interval):
                break
            waited += self.interval
            label = self._sample()
        self.result = label
        # Do not deliver a late result after ``stop()``/join timeout: the owner
        # may already have torn down the UI objects the callback touches.
        if self.on_result is not None and not self._stop.is_set():
            try:
                self.on_result(label)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def __enter__(self) -> RendererProbe:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
        self.join(timeout=1.0)


__all__ = [
    "RENDERER_CHOICES",
    "RENDERER_HELP",
    "STALE_DAYS",
    "VulkanInfo",
    "classify_maps",
    "detect_renderer",
    "dxvk_ok",
    "effective_renderer",
    "parse_vulkan_summary",
    "read_maps",
    "renderer_choice",
    "renderer_env",
    "reset_cache",
    "vulkan_info",
    "vulkan_note",
    "RendererProbe",
]
