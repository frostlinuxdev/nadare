# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Skip Wine's SDL gamepad scan when no gamepad is connected.

Every Wine start loads its controller driver (winebus), which starts SDL. Inside the Steam
Runtime container SDL cannot use udev ("Container detected, disabling udev integration") and
opens every ``/dev/input/event*`` to see whether it is a gamepad: once for joysticks, once for
force feedback. Closing an input device waits for a kernel RCU grace period: 48 ms a device on a
Debian kernel, 5 ms on a CachyOS one. With 25 input devices (sound-card jacks count) that was
~2 s of every Play on the test desktop (Endfield, Play -> window: 13.9 -> 11.9 s).

When no gamepad is connected Nadare sets winebus's own ``"Enable SDL"=dword:0`` in the prefix
before the start; winebus then uses its udev/evdev backend only. With a gamepad, or when Nadare
cannot tell, Wine's default (SDL on) is put back, so controller players keep SDL's mappings. A
pad plugged in while the game already runs then goes through the evdev backend (Xbox pads are
still XInput; others may get a generic mapping until the next Play).

``NADARE_WINEBUS_SDL=1`` (always SDL) or ``=0`` (never) in the environment or the profile's
Environment overrides the automatic choice.
"""

from __future__ import annotations

import logging
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from . import atomicfile

log = logging.getLogger(__name__)

ENV_CHOICE = "NADARE_WINEBUS_SDL"
INPUT_ROOT = Path("/sys/class/input")

#: Key codes that make an input device a gamepad or joystick for SDL and udev:
#: BTN_JOYSTICK..BTN_DEAD, BTN_GAMEPAD (BTN_SOUTH)..BTN_THUMBR, BTN_TRIGGER_HAPPY1..40.
_GAMEPAD_KEYS = ((0x120, 0x12F), (0x130, 0x13E), (0x2C0, 0x2E7))
#: KEY_Q..KEY_P, the top letter row: a device with all of them is a keyboard. Virtual input
#: devices (ydotoold, remote-desktop tools) claim every key code, gamepad buttons included.
_KEYBOARD_ROW = ((16, 25),)
_WORD_BITS = 64 if sys.maxsize > 2**32 else 32

_SECTION = "[System\\\\ControlSet001\\\\Services\\\\winebus]"
_NAME = '"Enable SDL"='
_OFF = '"Enable SDL"=dword:00000000'


def _bits(bitmap: str) -> list[int]:
    """The kernel's capability text (hex words, most significant first) as words, lowest
    first. Raises ``ValueError`` on text that is not such a bitmap."""
    words = [int(word, 16) for word in reversed(bitmap.split())]
    if not words:
        raise ValueError("empty bitmap")
    return words


def _has(words: list[int], code: int) -> bool:
    index, bit = divmod(code, _WORD_BITS)
    return index < len(words) and bool(words[index] >> bit & 1)


def _has_gamepad_keys(bitmap: str) -> bool:
    """Whether a ``capabilities/key`` text has a gamepad/joystick button."""
    words = _bits(bitmap)
    return any(_has(words, code) for low, high in _GAMEPAD_KEYS for code in range(low, high + 1))


def _is_keyboard(bitmap: str) -> bool:
    """Whether a ``capabilities/key`` text has the whole top letter row."""
    words = _bits(bitmap)
    return all(_has(words, code) for low, high in _KEYBOARD_ROW for code in range(low, high + 1))


def _is_gamepad(device: Path) -> bool:
    """Gamepad buttons, an absolute axis (sticks, a d-pad hat, a throttle) and not a keyboard:
    close to udev's joystick rule. Reads ``abs`` only for a device with gamepad buttons.
    Raises ``OSError``/``ValueError`` when the device cannot be read."""
    keys = (device / "capabilities" / "key").read_text()
    if not _has_gamepad_keys(keys) or _is_keyboard(keys):
        return False
    return any(_bits((device / "capabilities" / "abs").read_text()))


def gamepad_connected(root: Path | None = None) -> bool | None:
    """True when an input device is a gamepad or joystick (see ``_is_gamepad``), False when none
    is, None when it cannot be told (no sysfs, an unreadable device). Reads sysfs only: no
    device is opened."""
    root = INPUT_ROOT if root is None else root
    try:
        names = sorted(p.name for p in root.iterdir() if p.name.startswith("event"))
    except OSError:
        return None
    unknown = False
    for name in names:
        try:
            if _is_gamepad(root / name / "device"):
                return True
        except (OSError, ValueError):
            unknown = True
    return None if unknown else False


def _atomic_write(path: Path, text: str, mode: int) -> None:
    atomicfile.write(path, text, mode=mode)


def set_sdl(prefix: Path | str, enabled: bool) -> bool:
    """Turn winebus's SDL backend off (``enabled=False``) or back to Wine's default (the value
    removed) in ``prefix/system.reg``. True when the file changed. A prefix without the file or
    without winebus's key (not set up yet) is left alone, and so is a linked file."""
    # Proton's prefix is STEAM_COMPAT_DATA_PATH/pfx; Nadare links pfx to the folder itself, an
    # older or foreign prefix may have a real pfx folder.
    for path in (Path(prefix) / "system.reg", Path(prefix) / "pfx" / "system.reg"):
        try:
            info = path.lstat()
            break
        except OSError:
            continue
    else:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.split(" ", 1)[0].rstrip("\r\n") == _SECTION),
        None,
    )
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    found = next((i for i in range(start + 1, end) if lines[i].startswith(_NAME)), None)
    newline = lines[start][len(lines[start].rstrip("\r\n")) :] or "\n"
    if enabled:
        if found is None:
            return False
        del lines[found]
    else:
        if found is not None:
            if lines[found].rstrip("\r\n") == _OFF:
                return False
            lines[found] = _OFF + newline
        else:
            at = start + 1
            if at < len(lines) and lines[at].startswith("#time="):
                at += 1
            lines.insert(at, _OFF + newline)
    _atomic_write(path, "".join(lines), stat.S_IMODE(info.st_mode))
    return True


def prepare(prefix: Path | str, env: Mapping[str, str]) -> str | None:
    """Set winebus's SDL backend for this start; returns a line for the launch log.
    Never raises: a registry that cannot be changed leaves Wine as it was."""
    choice = str(env.get(ENV_CHOICE, "")).strip()
    if choice in ("0", "1"):
        enabled = choice == "1"
        why = f"{ENV_CHOICE}={choice}"
    else:
        pad = gamepad_connected()
        enabled = pad is not False
        why = (
            "gamepad connected"
            if pad
            else "could not check for a gamepad"
            if pad is None
            else "no gamepad connected"
        )
    try:
        set_sdl(prefix, enabled)
    except (OSError, UnicodeError) as exc:
        log.warning("winebus: could not change system.reg: %s", exc)
        return f"controllers: registry not changed ({exc})"
    if enabled:
        return f"controllers: {why}, full SDL controller support"
    return f"controllers: {why}, quick start (Wine's SDL gamepad scan skipped)"
