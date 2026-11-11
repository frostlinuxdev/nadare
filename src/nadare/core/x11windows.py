# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Read the X11 window tree with plain ``ctypes`` and libX11.

KWin under Wayland leaves ``_NET_CLIENT_LIST`` empty, so the tree is walked instead, down to
:data:`_DEPTH` levels: KWin puts a managed window inside a frame and a wrapper window (root,
frame, wrapper, client: measured). A window is "shown" when it is mapped (``IsViewable``) or
minimized. Missing windows while walking (BadWindow) are ignored.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from collections.abc import Iterable
from typing import Any

#: ``IsViewable`` from ``X.h``.
_IS_VIEWABLE = 2
#: ``IconicState`` from ``Xutil.h`` (the first field of ``WM_STATE``).
_ICONIC_STATE = 3
#: How many levels below the root are read (a KWin client window is at level 3).
_DEPTH = 4


class _XWindowAttributes(ctypes.Structure):
    """The part of ``XWindowAttributes`` needed to read ``map_state`` (full layout)."""

    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


@_XErrorHandler
def _ignore_error(_display: int, _event: int) -> int:
    """Swallow X errors for the duration of a walk (a window may vanish mid-walk)."""
    return 0


def _configure(lib: Any) -> None:
    """Set the prototypes; without them pointer-sized values would be truncated on 64-bit."""
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.restype = ctypes.c_int
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XQueryTree.restype = ctypes.c_int
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XFree.restype = ctypes.c_int
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_XWindowAttributes),
    ]
    lib.XGetWindowAttributes.restype = ctypes.c_int
    lib.XGetClassHint.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_XClassHint),
    ]
    lib.XGetClassHint.restype = ctypes.c_int
    lib.XFetchName.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.XFetchName.restype = ctypes.c_int


def _children(lib: Any, display: int, window: int) -> list[int]:
    """The direct children of ``window`` (an empty list when the query fails)."""
    root = ctypes.c_ulong()
    parent = ctypes.c_ulong()
    children = ctypes.POINTER(ctypes.c_ulong)()
    count = ctypes.c_uint()
    ok = lib.XQueryTree(
        display,
        window,
        ctypes.byref(root),
        ctypes.byref(parent),
        ctypes.byref(children),
        ctypes.byref(count),
    )
    if not ok:
        return []
    try:
        return [int(children[i]) for i in range(count.value)]
    finally:
        if children:
            lib.XFree(children)


def _property_text(lib: Any, display: int, window: int, atom: int) -> str | None:
    """A window property as text, or ``None`` when it is absent or not 8-bit."""
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    data = ctypes.c_void_p()
    status = lib.XGetWindowProperty(
        display,
        window,
        atom,
        0,
        1024,
        0,
        0,  # AnyPropertyType
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(data),
    )
    if status != 0 or not data:
        return None
    try:
        if actual_format.value != 8:
            return None
        return ctypes.string_at(data, nitems.value).decode("utf-8", "replace")
    finally:
        lib.XFree(data)


def _title(lib: Any, display: int, window: int, net_wm_name: int) -> str:
    """``_NET_WM_NAME`` (UTF-8) when set, else ``WM_NAME``."""
    text = _property_text(lib, display, window, net_wm_name)
    if text is not None:
        return text
    name = ctypes.c_void_p()
    if lib.XFetchName(display, window, ctypes.byref(name)) and name:
        try:
            return ctypes.string_at(name.value).decode("utf-8", "replace")
        finally:
            lib.XFree(name)
    return ""


def _window_class(lib: Any, display: int, window: int) -> str:
    """The class part of ``WM_CLASS`` (empty when the window has none)."""
    hint = _XClassHint()
    if not lib.XGetClassHint(display, window, ctypes.byref(hint)):
        return ""
    try:
        if hint.res_class:
            return ctypes.string_at(hint.res_class).decode("utf-8", "replace")
        return ""
    finally:
        if hint.res_name:
            lib.XFree(hint.res_name)
        if hint.res_class:
            lib.XFree(hint.res_class)


def is_shown(map_state: int, wm_state: int | None) -> bool:
    """Mapped, or minimized: a minimized window is unmapped too, but its ``WM_STATE`` says
    Iconic. A window closed to the tray is unmapped with no ``WM_STATE`` (measured on KWin)."""
    return map_state == _IS_VIEWABLE or wm_state == _ICONIC_STATE


def _wm_state(lib: Any, display: int, window: int, atom: int) -> int | None:
    """The first field of ``WM_STATE`` (1 normal, 3 iconic), or ``None`` without one."""
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    data = ctypes.c_void_p()
    status = lib.XGetWindowProperty(
        display,
        window,
        atom,
        0,
        2,
        0,
        atom,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(data),
    )
    if status != 0 or not data:
        return None
    try:
        if actual_format.value != 32 or nitems.value < 1:
            return None
        # Xlib hands 32-bit properties back as C longs.
        return int(ctypes.c_long.from_address(data.value).value)
    finally:
        lib.XFree(data)


def _viewable(lib: Any, display: int, window: int, wm_state_atom: int) -> bool:
    attrs = _XWindowAttributes()
    if not lib.XGetWindowAttributes(display, window, ctypes.byref(attrs)):
        return False
    return is_shown(attrs.map_state, _wm_state(lib, display, window, wm_state_atom))


def list_windows() -> list[tuple[str, str, bool]] | None:
    """``(WM_CLASS class, title, shown)`` for the windows down to :data:`_DEPTH` levels.

    "Shown" counts a minimized window too (see :func:`is_shown`).

    ``None`` when libX11 cannot be loaded or no display can be opened.
    """
    try:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        lib = ctypes.CDLL(name)
        _configure(lib)
    except (OSError, AttributeError):
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    previous = lib.XSetErrorHandler(ctypes.cast(_ignore_error, ctypes.c_void_p))
    try:
        root = lib.XDefaultRootWindow(display)
        windows: list[int] = []
        level = [root]
        for _ in range(_DEPTH):
            level = [child for parent in level for child in _children(lib, display, parent)]
            windows.extend(level)
        net_wm_name = lib.XInternAtom(display, b"_NET_WM_NAME", 0)
        wm_state = lib.XInternAtom(display, b"WM_STATE", 0)
        return [
            (
                _window_class(lib, display, window),
                _title(lib, display, window, net_wm_name),
                _viewable(lib, display, window, wm_state),
            )
            for window in windows
        ]
    finally:
        lib.XSetErrorHandler(previous)
        lib.XCloseDisplay(display)


def shown_from(
    windows: Iterable[tuple[str, str, bool]], titles: Iterable[str], wm_class: str = "steam_proton"
) -> bool:
    """True when a viewable window of ``wm_class`` has one of the exact ``titles``."""
    wanted = set(titles)
    return any(
        cls == wm_class and title in wanted and viewable
        for cls, title, viewable in windows
    )


def any_shown(titles: Iterable[str], wm_class: str = "steam_proton") -> bool | None:
    """Whether such a window is shown; ``None`` when that cannot be known."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        windows = list_windows()
    except Exception:
        return None
    if windows is None:
        return None
    return shown_from(windows, titles, wm_class)
