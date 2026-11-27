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
import threading
from collections.abc import Iterable
from typing import Any

#: One X11 tree walk at a time (see :func:`_list_windows_full`).
_WALK_LOCK = threading.Lock()

#: ``IsViewable`` from ``X.h``.
_IS_VIEWABLE = 2
#: ``IconicState`` from ``Xutil.h`` (the first field of ``WM_STATE``).
_ICONIC_STATE = 3
#: How many levels below the root are read (a KWin client window is at level 3).
_DEPTH = 4

#: ``ClientMessage`` from ``X.h``: the event type EWMH uses for ``_NET_CLOSE_WINDOW``.
_CLIENT_MESSAGE = 33
#: The two masks a client sends an EWMH root message with (``X.h``).
_SUBSTRUCTURE_REDIRECT = 1 << 20
_SUBSTRUCTURE_NOTIFY = 1 << 19


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


class _XClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _XClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class _XResClientIdSpec(ctypes.Structure):
    """``XResClientIdSpec`` from ``X11/extensions/XRes.h``."""

    _fields_ = [("client", ctypes.c_ulong), ("mask", ctypes.c_uint)]


class _XResClientIdValue(ctypes.Structure):
    """``XResClientIdValue`` from ``X11/extensions/XRes.h``."""

    _fields_ = [
        ("spec", _XResClientIdSpec),
        ("length", ctypes.c_long),
        ("value", ctypes.c_void_p),
    ]


#: ``XRES_CLIENT_ID_PID_MASK``: ask for the pid of the client (X-Resource 1.2).
_XRES_CLIENT_ID_PID_MASK = 1 << 1


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
    lib.XSendEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_long,
        ctypes.POINTER(_XEvent),
    ]
    lib.XSendEvent.restype = ctypes.c_int
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XFlush.restype = ctypes.c_int


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


def _net_wm_pid(lib: Any, display: int, window: int, atom: int) -> int | None:
    """``_NET_WM_PID`` (the owning process), or ``None`` when the window has none.

    EWMH recommends it and Wine's X11 driver, Qt, GTK and SDL normally set it; without
    it a window's owner cannot be proven, so callers treat the window as unknown rather
    than guessing.
    """
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
        1,
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
        if actual_format.value != 32 or nitems.value < 1:
            return None
        pid = int(ctypes.c_long.from_address(data.value).value)
        return pid or None
    finally:
        lib.XFree(data)


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
    return _viewable_size(lib, display, window, wm_state_atom)[0]


def _viewable_size(
    lib: Any, display: int, window: int, wm_state_atom: int
) -> tuple[bool, tuple[int, int]]:
    attrs = _XWindowAttributes()
    if not lib.XGetWindowAttributes(display, window, ctypes.byref(attrs)):
        return False, (0, 0)
    shown = is_shown(attrs.map_state, _wm_state(lib, display, window, wm_state_atom))
    return shown, (int(attrs.width), int(attrs.height))


def list_windows() -> list[tuple[str, str, bool]] | None:
    """``(WM_CLASS class, title, shown)`` for the windows down to :data:`_DEPTH` levels.

    "Shown" counts a minimized window too (see :func:`is_shown`).

    ``None`` when libX11 cannot be loaded or no display can be opened.
    """
    found = list_windows_sized()
    return None if found is None else [(cls, title, shown) for cls, title, shown, _ in found]


def _list_windows_full() -> (
    list[tuple[int, str, str, int | None, bool, tuple[int, int]]] | None
):
    """The tree walk's ``(id, class, title, _NET_WM_PID, shown, (width, height))``.

    ``None`` when libX11 cannot be loaded or no display can be opened. One walk at a
    time: ``XSetErrorHandler`` is process-wide, so a second walk could restore the
    default handler (which exits on ``BadWindow``) in the middle of the first.
    """
    with _WALK_LOCK:
        return _walk_windows()


def _walk_windows() -> (
    list[tuple[int, str, str, int | None, bool, tuple[int, int]]] | None
):
    """The tree walk's ``(id, class, title, _NET_WM_PID, shown, (width, height))``.

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
        net_wm_pid = lib.XInternAtom(display, b"_NET_WM_PID", 0)
        out = []
        for window in windows:
            shown, size = _viewable_size(lib, display, window, wm_state)
            out.append((
                window,
                _window_class(lib, display, window),
                _title(lib, display, window, net_wm_name),
                _net_wm_pid(lib, display, window, net_wm_pid),
                shown,
                size,
            ))
        return out
    finally:
        lib.XSetErrorHandler(previous)
        lib.XCloseDisplay(display)


def list_windows_sized() -> list[tuple[str, str, bool, tuple[int, int]]] | None:
    """:func:`list_windows` plus each window's ``(width, height)``."""
    found = _list_windows_full()
    return (
        None
        if found is None
        else [(cls, title, shown, size) for _id, cls, title, _pid, shown, size in found]
    )


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


#: Smaller windows are Wine's helpers (IME, 1x1 message windows, menu strips), not something
#: the player sees.
_MIN_SIDE = 64


def window_open_from(
    windows: Iterable[tuple[str, str, bool, tuple[int, int]]], wm_class: str = "steam_proton"
) -> bool:
    """True when a viewable ``wm_class`` window of real size exists, whatever its title."""
    return any(
        cls == wm_class and shown and size[0] >= _MIN_SIDE and size[1] >= _MIN_SIDE
        for cls, _title, shown, size in windows
    )


def any_window_open(wm_class: str = "steam_proton") -> bool | None:
    """Whether any such window is open (a titleless prompt counts); ``None`` when unknown."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        windows = list_windows_sized()
    except Exception:
        return None
    if windows is None:
        return None
    return window_open_from(windows, wm_class)


def list_windows_owned() -> list[tuple[int, int]] | None:
    """``(window id, owning pid)`` for each shown window a player could see.

    Same rules as :func:`window_open_from`: the window must be shown and at least
    :data:`_MIN_SIDE` on both sides, so Wine's 1x1 IME/message helper windows are not
    mistaken for a game. A window without ``_NET_WM_PID`` is left out: its owner is not
    known, and no caller may guess. ``None`` when libX11 cannot be loaded or no display
    can be opened.
    """
    found = _list_windows_full()
    if found is None:
        return None
    return [
        (window, pid)
        for window, _cls, _title, pid, shown, (width, height) in found
        if shown and pid is not None and width >= _MIN_SIDE and height >= _MIN_SIDE
    ]


def _send_close(lib: Any, display: int, root: int, window: int, atom: int) -> bool:
    """Send one EWMH ``_NET_CLOSE_WINDOW`` message for ``window`` to the root window."""
    event = _XEvent()
    event.xclient.type = _CLIENT_MESSAGE
    event.xclient.send_event = 1
    event.xclient.display = display
    event.xclient.window = window
    event.xclient.message_type = atom
    event.xclient.format = 32
    event.xclient.data.l[0] = 0  # CurrentTime: the window manager supplies one
    event.xclient.data.l[1] = 1  # source indication: a normal application
    mask = _SUBSTRUCTURE_REDIRECT | _SUBSTRUCTURE_NOTIFY
    return bool(lib.XSendEvent(display, root, 0, mask, ctypes.byref(event)))


def request_close(windows: Iterable[int]) -> int | None:
    """Ask the window manager to close each window (EWMH ``_NET_CLOSE_WINDOW``).

    This is the polite stop for a Sandboxed game: the request goes through the
    desktop's own window manager, so no Wine helper has to start outside the
    Sandbox. Returns the number of messages sent, or ``None`` when libX11 or the
    display is unavailable. The window manager decides whether to close; the
    callers still wait for the game to exit and escalate afterwards.
    """
    ids = [int(window) for window in windows if int(window)]
    if not ids:
        return 0
    if not os.environ.get("DISPLAY"):
        return None
    try:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        lib = ctypes.CDLL(name)
        _configure(lib)
    except (OSError, AttributeError):
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    # XSetErrorHandler is process-wide: a walk must not restore its handler mid-send.
    with _WALK_LOCK:
        previous = lib.XSetErrorHandler(ctypes.cast(_ignore_error, ctypes.c_void_p))
        try:
            root = lib.XDefaultRootWindow(display)
            atom = lib.XInternAtom(display, b"_NET_CLOSE_WINDOW", 0)
            if not atom:
                return None
            sent = sum(_send_close(lib, display, root, window, atom) for window in ids)
            lib.XFlush(display)
            return sent
        finally:
            lib.XSetErrorHandler(previous)
            lib.XCloseDisplay(display)


def _load_xres() -> Any | None:
    """libXRes with the calls :func:`client_pids` makes, or ``None`` when it is missing."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("XRes") or "libXRes.so.1")
        lib.XResQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.XResQueryExtension.restype = ctypes.c_int
        lib.XResQueryVersion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.XResQueryVersion.restype = ctypes.c_int
        lib.XResQueryClientIds.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.POINTER(_XResClientIdSpec),
            ctypes.POINTER(ctypes.c_long),
            ctypes.POINTER(ctypes.POINTER(_XResClientIdValue)),
        ]
        lib.XResQueryClientIds.restype = ctypes.c_int
        lib.XResGetClientPid.argtypes = [ctypes.POINTER(_XResClientIdValue)]
        lib.XResGetClientPid.restype = ctypes.c_int
        lib.XResClientIdsDestroy.argtypes = [
            ctypes.c_long,
            ctypes.POINTER(_XResClientIdValue),
        ]
        lib.XResClientIdsDestroy.restype = None
    except (OSError, AttributeError):
        return None
    return lib


def _client_pid(xres: Any, display: int, window: int) -> int | None:
    """The pid of the X client that owns ``window``, as the X server knows it."""
    spec = _XResClientIdSpec(window, _XRES_CLIENT_ID_PID_MASK)
    count = ctypes.c_long(0)
    values = ctypes.POINTER(_XResClientIdValue)()
    # One window per request: the reply names the client's resource base, not the window.
    status = xres.XResQueryClientIds(
        display, 1, ctypes.byref(spec), ctypes.byref(count), ctypes.byref(values)
    )
    if status != 0 or not values:  # Success is 0 here
        return None
    try:
        for index in range(count.value):
            pid = xres.XResGetClientPid(ctypes.byref(values[index]))
            if pid > 0:
                return int(pid)
        return None
    finally:
        xres.XResClientIdsDestroy(count, values)


def client_pids(windows: Iterable[int]) -> dict[int, int] | None:
    """``{window: pid}`` of the X client that made each window (X-Resource extension).

    The pid is the one the X server sees, so in the host's PID namespace: unlike
    ``_NET_WM_PID``, which a Sandboxed game fills with a namespace-local pid that another
    Sandboxed game can share. A window whose client is unknown (gone, remote, or the
    server cannot tell) is left out. ``None`` when libXRes, the display or the extension
    (1.2 or newer) is unavailable.
    """
    ids = [int(window) for window in windows if int(window)]
    if not ids:
        return {}
    if not os.environ.get("DISPLAY"):
        return None
    xres = _load_xres()
    if xres is None:
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        _configure(lib)
    except (OSError, AttributeError):
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    with _WALK_LOCK:
        previous = lib.XSetErrorHandler(ctypes.cast(_ignore_error, ctypes.c_void_p))
        try:
            event_base, error_base = ctypes.c_int(), ctypes.c_int()
            if not xres.XResQueryExtension(
                display, ctypes.byref(event_base), ctypes.byref(error_base)
            ):
                return None
            major, minor = ctypes.c_int(), ctypes.c_int()
            if not xres.XResQueryVersion(display, ctypes.byref(major), ctypes.byref(minor)):
                return None
            if (major.value, minor.value) < (1, 2):
                return None
            found: dict[int, int] = {}
            for window in ids:
                pid = _client_pid(xres, display, window)
                if pid is not None:
                    found[window] = pid
            return found
        finally:
            lib.XSetErrorHandler(previous)
            lib.XCloseDisplay(display)
