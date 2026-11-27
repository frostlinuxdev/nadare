# SPDX-License-Identifier: GPL-3.0-or-later
"""Two costs every Proton Play paid for nothing (measured on Endfield, Play -> window).

1. Wine's controller driver (winebus) starts SDL, and SDL inside the Steam Runtime container
   opens every ``/dev/input/event*`` twice to look for gamepads. Each close of an input device
   waits for a kernel RCU grace period: 48 ms a device on a Debian kernel (25 devices, two
   passes: ~2 s of every Play; 13.9 -> 11.9 s), 5 ms on a CachyOS kernel. With no gamepad
   connected Nadare now turns winebus's SDL backend off for that start. With a gamepad (or when
   it cannot tell) SDL stays on, so controller players keep the full mapping.
2. DW-Proton turns its FSR4 upgrade on for every game and downloads a manifest from GitHub on
   every launch (10 s timeout). The upgrade only does anything by default on RDNA4 GPUs (FP8):
   elsewhere Nadare sets ``PROTON_FSR4_UPGRADE=0``. A value the user set always wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import fsr4, winebus

# ---------------------------------------------------------------- gamepad detection (sysfs)


def _key_bitmap(*codes: int) -> str:
    """The kernel's ``capabilities/key`` text: 64-bit words, most significant first."""
    if not codes:
        return "0"
    words = [0] * (max(codes) // 64 + 1)
    for code in codes:
        words[code // 64] |= 1 << (code % 64)
    return " ".join(format(w, "x") for w in reversed(words))


def _input_root(
    tmp_path: Path, devices: dict[str, str | None | tuple[str | None, str | None]]
) -> Path:
    """A fake ``/sys/class/input``. A device is its ``capabilities/key`` text, or a
    ``(key, abs)`` pair; a bare key text gets no absolute axes (``abs`` = ``0``). None = no file."""
    root = tmp_path / "input"
    root.mkdir()
    for name, caps_text in devices.items():
        keys, axes = caps_text if isinstance(caps_text, tuple) else (caps_text, "0")
        caps = root / name / "device" / "capabilities"
        caps.mkdir(parents=True)
        if keys is not None:
            (caps / "key").write_text(keys + "\n")
        if axes is not None:
            (caps / "abs").write_text(axes + "\n")
    return root


STICKS = "3"  # ABS_X, ABS_Y
PAD_AXES = format((1 << 0) | (1 << 1) | (1 << 3) | (1 << 4) | (1 << 0x10) | (1 << 0x11), "x")


KEYBOARD = _key_bitmap(1, 30, 57)  # KEY_ESC, KEY_A, KEY_SPACE
MOUSE = _key_bitmap(0x110, 0x111, 0x112)  # BTN_LEFT/RIGHT/MIDDLE
HDA_JACK = "0"


def test_keyboards_mice_and_sound_jacks_are_not_gamepads(tmp_path):
    root = _input_root(
        tmp_path,
        {"event0": KEYBOARD, "event1": MOUSE, "event2": HDA_JACK, "mouse0": None, "js": None},
    )
    assert winebus.gamepad_connected(root) is False


@pytest.mark.parametrize(
    "code",
    [
        0x130,  # BTN_SOUTH (BTN_GAMEPAD): Xbox / PlayStation / Switch pads
        0x13E,  # BTN_THUMBR
        0x120,  # BTN_TRIGGER (BTN_JOYSTICK): flight sticks, many wheels
        0x12F,  # BTN_DEAD
        0x2C0,  # BTN_TRIGGER_HAPPY1: extra pad buttons
    ],
)
def test_a_gamepad_or_joystick_button_counts(tmp_path, code):
    root = _input_root(tmp_path, {"event0": KEYBOARD, "event7": (_key_bitmap(code), STICKS)})
    assert winebus.gamepad_connected(root) is True


def test_the_word_boundary_is_read_right(tmp_path):
    # 0x130 is bit 48 of the 5th 64-bit word: a misread word order would miss it.
    keys = _key_bitmap(0x130, 1)
    assert len(keys.split()) == 5
    root = _input_root(tmp_path, {"event3": (keys, STICKS)})
    assert winebus.gamepad_connected(root) is True


# Virtual keyboards claim every key code, gamepad buttons included; udev calls them keyboards.
# Seen on an NVIDIA PC (2026-11-19): with ydotoold running every Play said "gamepad connected".
YDOTOOLD = (
    "ffffffffff 0 ffffff0003007f 1000f7fffffff 7fe001fffff000f 7ffffffffffffff "
    "ffffffff0003fdff 7fff8fff00ff03ff 1fffffffffff"
)
KEYD_KEYBOARD = (
    "10000000000000 0 ffffffffffffff0f ffffffffffffffff ffffffffffffffff fffffffffffffffe"
)
KEYD_POINTER = ("ff0000 0 0 0 0", "3")
XBOX_PAD = _key_bitmap(0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x13A, 0x13B, 0x13C, 0x13D, 0x13E)


def test_ydotoold_s_virtual_device_is_not_a_gamepad(tmp_path):
    root = _input_root(
        tmp_path, {"event20": YDOTOOLD, "event21": KEYD_KEYBOARD, "event22": KEYD_POINTER}
    )
    assert winebus.gamepad_connected(root) is False


def test_a_real_pad_next_to_a_virtual_keyboard_still_counts(tmp_path):
    root = _input_root(tmp_path, {"event20": YDOTOOLD, "event25": (XBOX_PAD, PAD_AXES)})
    assert winebus.gamepad_connected(root) is True


def test_gamepad_buttons_without_any_axis_are_not_a_gamepad(tmp_path):
    root = _input_root(tmp_path, {"event7": XBOX_PAD})
    assert winebus.gamepad_connected(root) is False


def test_a_full_keyboard_with_axes_is_still_a_keyboard(tmp_path):
    # remote-desktop and drawing tools make one device with every key plus an absolute pointer
    root = _input_root(tmp_path, {"event9": (YDOTOOLD, STICKS)})
    assert winebus.gamepad_connected(root) is False


def test_a_pad_whose_axes_cannot_be_read_means_unknown(tmp_path):
    root = _input_root(tmp_path, {"event7": (XBOX_PAD, None)})
    assert winebus.gamepad_connected(root) is None


def test_a_keyboard_is_never_asked_for_its_axes(tmp_path):
    # only a device with gamepad buttons needs its axes read; a missing file elsewhere is fine
    root = _input_root(tmp_path, {"event0": (KEYBOARD, None), "event1": (MOUSE, None)})
    assert winebus.gamepad_connected(root) is False


def test_a_device_whose_capabilities_cannot_be_read_means_unknown(tmp_path):
    root = _input_root(tmp_path, {"event0": KEYBOARD, "event1": None})
    assert winebus.gamepad_connected(root) is None


def test_garbage_in_the_bitmap_means_unknown(tmp_path):
    root = _input_root(tmp_path, {"event0": "zz 12"})
    assert winebus.gamepad_connected(root) is None


def test_no_sysfs_means_unknown(tmp_path):
    assert winebus.gamepad_connected(tmp_path / "missing") is None


def test_no_input_devices_at_all_means_no_gamepad(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    assert winebus.gamepad_connected(root) is False


# ---------------------------------------------------------------- the registry switch

SECTION = "[System\\\\ControlSet001\\\\Services\\\\winebus] 1794582520\n"
WINEBUS = (
    SECTION
    + "#time=1dd7380ddbcf754\n"
    + '"Description"="Wine HID bus driver"\n'
    + '"DisplayName"="Wine HID bus"\n'
    + '"ImagePath"=str(2):"C:\\\\windows\\\\system32\\\\drivers\\\\winebus.sys"\n'
)
BEFORE = "WINE REGISTRY Version 2\n;; All keys relative to \\\\Machine\n\n[Software\\\\Wine] 1\n\n"
AFTER = '\n[System\\\\ControlSet001\\\\Services\\\\winebus\\\\Enum] 1\n"0"="x"\n'
OFF = '"Enable SDL"=dword:00000000\n'


def _prefix(tmp_path: Path, body: str = WINEBUS) -> Path:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    reg = prefix / "system.reg"
    reg.write_text(BEFORE + body + AFTER)
    reg.chmod(0o640)
    return prefix


def _reg(prefix: Path) -> str:
    return (prefix / "system.reg").read_text()


def test_turning_sdl_off_adds_the_value_to_winebus_only(tmp_path):
    prefix = _prefix(tmp_path)
    assert winebus.set_sdl(prefix, False) is True
    text = _reg(prefix)
    assert (
        text
        == BEFORE + SECTION + "#time=1dd7380ddbcf754\n" + OFF + WINEBUS[len(SECTION) + 22 :] + AFTER
    )
    assert text.count("Enable SDL") == 1
    assert (prefix / "system.reg").stat().st_mode & 0o777 == 0o640


def test_turning_it_off_twice_changes_nothing(tmp_path):
    prefix = _prefix(tmp_path)
    winebus.set_sdl(prefix, False)
    first = _reg(prefix)
    assert winebus.set_sdl(prefix, False) is False
    assert _reg(prefix) == first


def test_turning_sdl_on_removes_the_value_again(tmp_path):
    prefix = _prefix(tmp_path)
    winebus.set_sdl(prefix, False)
    assert winebus.set_sdl(prefix, True) is True
    assert _reg(prefix) == BEFORE + WINEBUS + AFTER


def test_on_is_a_no_op_when_wine_s_default_is_in_place(tmp_path):
    prefix = _prefix(tmp_path)
    assert winebus.set_sdl(prefix, True) is False
    assert _reg(prefix) == BEFORE + WINEBUS + AFTER


def test_an_explicit_one_is_rewritten_to_zero(tmp_path):
    prefix = _prefix(tmp_path, WINEBUS + '"Enable SDL"=dword:00000001\n')
    assert winebus.set_sdl(prefix, False) is True
    assert _reg(prefix).count("Enable SDL") == 1
    assert OFF in _reg(prefix)


def test_a_value_in_another_key_is_left_alone(tmp_path):
    # Only winebus's own key: a same-named value in the Enum subkey stays.
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    other = (
        "\n[System\\\\ControlSet001\\\\Services\\\\winebus\\\\Enum] 1\n"
        '"Enable SDL"=dword:00000000\n'
    )
    (prefix / "system.reg").write_text(BEFORE + WINEBUS + other)
    assert winebus.set_sdl(prefix, True) is False
    assert (prefix / "system.reg").read_text() == BEFORE + WINEBUS + other


def test_a_prefix_without_system_reg_is_not_touched(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    assert winebus.set_sdl(prefix, False) is False
    assert not (prefix / "system.reg").exists()


def test_a_registry_without_the_winebus_key_is_not_touched(tmp_path):
    prefix = _prefix(tmp_path, "")
    before = _reg(prefix)
    assert winebus.set_sdl(prefix, False) is False
    assert _reg(prefix) == before


def test_a_linked_system_reg_is_not_followed(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    real = tmp_path / "elsewhere.reg"
    real.write_text(BEFORE + WINEBUS + AFTER)
    (prefix / "system.reg").symlink_to(real)
    assert winebus.set_sdl(prefix, False) is False
    assert real.read_text() == BEFORE + WINEBUS + AFTER


# ---------------------------------------------------------------- prepare(): the decision


@pytest.fixture
def pad(monkeypatch):
    state = {"value": False}
    monkeypatch.setattr(winebus, "gamepad_connected", lambda *a, **k: state["value"])
    return state


def test_no_gamepad_turns_sdl_off(tmp_path, pad):
    prefix = _prefix(tmp_path)
    note = winebus.prepare(prefix, {})
    assert OFF in _reg(prefix)
    assert note and "no gamepad" in note


def test_a_gamepad_keeps_sdl_on(tmp_path, pad):
    prefix = _prefix(tmp_path)
    winebus.prepare(prefix, {})
    pad["value"] = True
    note = winebus.prepare(prefix, {})
    assert "Enable SDL" not in _reg(prefix)
    assert note and "gamepad" in note


def test_unknown_keeps_sdl_on(tmp_path, pad):
    prefix = _prefix(tmp_path)
    winebus.set_sdl(prefix, False)
    pad["value"] = None
    winebus.prepare(prefix, {})
    assert "Enable SDL" not in _reg(prefix)


@pytest.mark.parametrize("value,expect_off", [("1", False), ("0", True)])
def test_the_users_choice_wins(tmp_path, pad, value, expect_off):
    pad["value"] = not expect_off  # the opposite of what auto would do
    prefix = _prefix(tmp_path)
    if not expect_off:
        winebus.set_sdl(prefix, False)
    winebus.prepare(prefix, {winebus.ENV_CHOICE: value})
    assert (OFF in _reg(prefix)) is expect_off


def test_a_broken_registry_never_stops_the_launch(tmp_path, pad, monkeypatch):
    prefix = _prefix(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(winebus, "_atomic_write", boom)
    winebus.prepare(prefix, {})  # no exception
    assert "Enable SDL" not in _reg(prefix)


# ---------------------------------------------------------------- FSR4: which GPUs


def _drm(tmp_path: Path, cards: dict[str, tuple[str, int | None]]) -> Path:
    root = tmp_path / "drm"
    root.mkdir()
    for name, (vendor, gc_major) in cards.items():
        dev = root / name / "device"
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(vendor + "\n")
        if gc_major is not None:
            gc = dev / "ip_discovery" / "die" / "0" / "GC" / "0"
            gc.mkdir(parents=True)
            (gc / "major").write_text(f"{gc_major}\n")
        # connector entries look like cards but are not
        (root / f"{name}-DP-1").mkdir()
    (root / "renderD128").mkdir()
    return root


@pytest.mark.parametrize(
    "cards,kinds",
    [
        ({"card0": ("0x8086", None), "card1": ("0x1002", 11)}, {"intel", "amd"}),
        ({"card0": ("0x1002", 12)}, {"amd-rdna4"}),
        ({"card0": ("0x1002", 13)}, {"amd-rdna4"}),
        ({"card0": ("0x1002", None)}, {"amd"}),
        ({"card0": ("0x10de", None)}, {"nvidia"}),
        ({"card0": ("0x1af4", None)}, {"other"}),
    ],
)
def test_gpu_kinds(tmp_path, cards, kinds):
    assert fsr4.gpu_kinds(_drm(tmp_path, cards)) == kinds


def test_no_cards_means_unknown(tmp_path):
    root = tmp_path / "drm"
    root.mkdir()
    assert fsr4.gpu_kinds(root) is None
    assert fsr4.gpu_kinds(tmp_path / "missing") is None


@pytest.mark.parametrize(
    "kinds,expected",
    [
        ({"nvidia"}, {"PROTON_FSR4_UPGRADE": "0"}),
        ({"intel"}, {"PROTON_FSR4_UPGRADE": "0"}),
        ({"amd"}, {"PROTON_FSR4_UPGRADE": "0"}),
        ({"intel", "amd"}, {"PROTON_FSR4_UPGRADE": "0"}),
        ({"amd-rdna4"}, {}),
        ({"intel", "amd-rdna4"}, {}),  # a laptop with an RX 9000: keep it
        ({"other"}, {}),  # a GPU we do not know: Proton decides
        ({"nvidia", "other"}, {}),
        (None, {}),
    ],
)
def test_upgrade_env(monkeypatch, kinds, expected):
    monkeypatch.setattr(fsr4, "gpu_kinds", lambda *a, **k: kinds)
    assert fsr4.upgrade_env({}) == expected


@pytest.mark.parametrize("value", ["1", "0", "4.1.1"])
def test_a_value_the_user_set_is_never_touched(monkeypatch, value):
    monkeypatch.setattr(fsr4, "gpu_kinds", lambda *a, **k: {"nvidia"})
    assert fsr4.upgrade_env({"PROTON_FSR4_UPGRADE": value}) == {}


# ---------------------------------------------------------------- wired into every Proton launch


@pytest.fixture
def backend(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.direct import EndfieldBackend
    from nadare.backends.winegame_parts import launching
    from nadare.core import protoncmd

    def make(profile_extra=None):
        profile = {"prefix": str(tmp_path / "prefix")}
        if profile_extra:
            profile.update(profile_extra)
        b = EndfieldBackend(make_context(tmp_path, "direct-endfield", profile))
        runner = tmp_path / "runner"
        runner.mkdir(exist_ok=True)
        monkeypatch.setattr(b, "resolve_runner", lambda: (runner, "proton"))
        return b

    monkeypatch.setattr(protoncmd.umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(launching, "_refuse_if_userns_blocked", lambda: None)
    monkeypatch.setattr(protoncmd.umu, "note_first_runtime", lambda emit: None)
    monkeypatch.setattr(launching.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(protoncmd.pvlocales, "prepare", lambda env, cache, **kw: {})
    monkeypatch.setattr(protoncmd.fsr4, "gpu_kinds", lambda *a, **k: {"nvidia"})
    monkeypatch.setattr(protoncmd.winebus, "gamepad_connected", lambda *a, **k: False)
    monkeypatch.delenv("PROTON_FSR4_UPGRADE", raising=False)
    monkeypatch.delenv(winebus.ENV_CHOICE, raising=False)
    return make


def test_a_proton_launch_skips_fsr4_and_the_sdl_scan(tmp_path, backend):
    prefix = _prefix(tmp_path)
    b = backend()
    _argv, env = b._prefix_command(["game.exe"])
    assert env["PROTON_FSR4_UPGRADE"] == "0"
    assert OFF in _reg(prefix)


def test_the_profile_environment_still_wins(tmp_path, backend):
    prefix = _prefix(tmp_path)
    b = backend({"extra_env": {"PROTON_FSR4_UPGRADE": "1", winebus.ENV_CHOICE: "1"}})
    _argv, env = b._prefix_command(["game.exe"])
    assert env["PROTON_FSR4_UPGRADE"] == "1"
    assert "Enable SDL" not in _reg(prefix)


def test_an_exported_value_still_wins(tmp_path, backend, monkeypatch):
    _prefix(tmp_path)
    monkeypatch.setenv("PROTON_FSR4_UPGRADE", "4.1.1")
    _argv, env = backend()._prefix_command(["game.exe"])
    assert env["PROTON_FSR4_UPGRADE"] == "4.1.1"


def test_a_prefix_with_a_real_pfx_folder_is_changed_there(tmp_path):
    data = tmp_path / "compat"
    (data / "pfx").mkdir(parents=True)
    (data / "pfx" / "system.reg").write_text(BEFORE + WINEBUS + AFTER)
    assert winebus.set_sdl(data, False) is True
    assert OFF in (data / "pfx" / "system.reg").read_text()


def test_the_suite_never_reads_the_real_machine():
    # conftest points both probes at a missing folder: "cannot tell" everywhere.
    assert fsr4.gpu_kinds() is None
    assert winebus.gamepad_connected() is None


def test_connector_entries_are_never_counted_as_cards(tmp_path):
    root = _drm(tmp_path, {"card0": ("0x1002", 12)})
    fake = root / "card0-DP-1" / "device"
    fake.mkdir()
    (fake / "vendor").write_text("0x10de\n")
    assert fsr4.gpu_kinds(root) == {"amd-rdna4"}
