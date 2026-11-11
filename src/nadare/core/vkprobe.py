# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Ask the Vulkan driver which GPUs there are, without ``vulkaninfo``.

Runs as its own short process (``nadare _vkprobe``): creating a Vulkan instance loads the GPU
driver, which must not happen inside the window's process (a broken driver could hang or crash
it). Prints one JSON object: ``{"instance": "1.4.321", "devices": [{"name", "api", "type",
"vendor"}]}``. Exit 0 on an answer, :data:`NO_VULKAN` when there is no usable Vulkan loader.

Only Vulkan 1.0 core calls the loader exports itself are used: ``vkCreateInstance``,
``vkEnumeratePhysicalDevices``, ``vkGetPhysicalDeviceProperties``, ``vkDestroyInstance``.
"""

from __future__ import annotations

import ctypes
import json
import sys

SUBCOMMAND = "_vkprobe"
NO_VULKAN = 3

_INSTANCE_CREATE_INFO = 1  # VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO
_MAX_DEVICES = 16
#: VkPhysicalDeviceProperties is ~824 bytes; read it into a generous buffer and only use the
#: fixed leading fields: apiVersion, driverVersion, vendorID, deviceID, deviceType, deviceName[256].
_PROPS_BUFFER = 4096
_NAME_OFFSET = 20
_NAME_SIZE = 256
_TYPES = {0: "other", 1: "integrated", 2: "discrete", 3: "virtual", 4: "cpu"}


class _InstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_uint32),
        ("pNext", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
        ("pApplicationInfo", ctypes.c_void_p),
        ("enabledLayerCount", ctypes.c_uint32),
        ("ppEnabledLayerNames", ctypes.c_void_p),
        ("enabledExtensionCount", ctypes.c_uint32),
        ("ppEnabledExtensionNames", ctypes.c_void_p),
    ]


def command() -> list[str]:
    """How to start the probe process from this build (frozen bundle or source)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, SUBCOMMAND]
    return [sys.executable, "-m", "nadare", SUBCOMMAND]


def device_type(value: int) -> str:
    return _TYPES.get(value, "other")


def version_text(value: int) -> str:
    return f"{value >> 22}.{(value >> 12) & 0x3FF}.{value & 0xFFF}"


def probe() -> dict | None:
    """The devices the Vulkan loader enumerates, or ``None`` without a usable loader."""
    try:
        lib = ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return None
    instance_version = None
    try:
        enum_version = lib.vkEnumerateInstanceVersion
        enum_version.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        enum_version.restype = ctypes.c_int32
        raw = ctypes.c_uint32()
        if enum_version(ctypes.byref(raw)) == 0:
            instance_version = version_text(raw.value)
    except AttributeError:
        pass  # a Vulkan 1.0 loader

    create = lib.vkCreateInstance
    create.argtypes = [
        ctypes.POINTER(_InstanceCreateInfo),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create.restype = ctypes.c_int32
    enumerate_devices = lib.vkEnumeratePhysicalDevices
    enumerate_devices.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    enumerate_devices.restype = ctypes.c_int32
    properties = lib.vkGetPhysicalDeviceProperties
    properties.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    properties.restype = None
    destroy = lib.vkDestroyInstance
    destroy.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    destroy.restype = None

    info = _InstanceCreateInfo(sType=_INSTANCE_CREATE_INFO)
    instance = ctypes.c_void_p()
    if create(ctypes.byref(info), None, ctypes.byref(instance)) != 0 or not instance.value:
        return None
    devices: list[dict] = []
    try:
        count = ctypes.c_uint32(_MAX_DEVICES)
        handles = (ctypes.c_void_p * _MAX_DEVICES)()
        result = enumerate_devices(instance, ctypes.byref(count), handles)
        if result not in (0, 5):  # VK_SUCCESS, VK_INCOMPLETE
            return {"instance": instance_version, "devices": []}
        for index in range(min(count.value, _MAX_DEVICES)):
            buffer = ctypes.create_string_buffer(_PROPS_BUFFER)
            properties(handles[index], buffer)
            fields = (ctypes.c_uint32 * 5).from_buffer(buffer)
            name = buffer.raw[_NAME_OFFSET : _NAME_OFFSET + _NAME_SIZE].split(b"\0", 1)[0]
            devices.append(
                {
                    "name": name.decode("utf-8", "replace"),
                    "api": version_text(fields[0]),
                    "type": device_type(fields[4]),
                    "vendor": int(fields[2]),
                }
            )
    finally:
        destroy(instance, None)
    return {"instance": instance_version, "devices": devices}


def main() -> int:
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 - report, never a traceback the caller must parse
        print(f"vulkan probe failed: {exc}", file=sys.stderr)
        return NO_VULKAN
    if result is None:
        return NO_VULKAN
    print(json.dumps(result))
    return 0


__all__ = ["NO_VULKAN", "SUBCOMMAND", "command", "device_type", "main", "probe", "version_text"]
