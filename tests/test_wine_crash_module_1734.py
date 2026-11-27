# SPDX-License-Identifier: GPL-3.0-or-later
"""Handwritten Wine/winedbg crash and module-table output (#1734)."""

import time

import pytest

from nadare.core.events import Event, mask

CRASH = (
    "wine: Unhandled page fault on read access to 000000000000036E at address "
    "00006FFFFFC174D7 (thread 0024), starting debugger..."
)
MODULES = [
    "Modules:",
    "Module  Address                                 Debug info      Name (154 modules)",
    "PE\t0000000140000000-000000014000e000\tDeferred        game.exe",
    "PE-Wine\t00006fffffb00000-00006fffffd00000\tExport          unityplayer.dll",
    "ELF\t00007ffff7000000-00007ffff7100000\tDwarf           libwine.so.1",
]


def watch(lines):
    from nadare.core.winecrash import WineCrash

    result = WineCrash()
    for line in lines:
        result.feed(mask(line))
    return result


@pytest.mark.parametrize(
    "line",
    [
        CRASH,
        CRASH.removeprefix("wine: "),
        "wine: Unhandled exception 0xc0000005 in thread 0024 at address 00006FFFFFC174D7 "
        "(thread 0024), starting debugger...",
        "Unhandled exception: page fault on read access to 0x000000000000036e in 64-bit code "
        "(0x00006fffffc174d7).",
    ],
)
@pytest.mark.parametrize("modules_first", [False, True])
def test_faulting_instruction_maps_to_module(line, modules_first):
    result = watch([*MODULES, line] if modules_first else [line, *MODULES])
    assert result.detected
    assert result.module == "unityplayer.dll"


def test_32_bit_exception_uses_instruction_not_access_address():
    result = watch(
        [
            "Unhandled exception: page fault on write access to 0x00000000 in 32-bit code "
            "(0x7bc01234).",
            "PE 00400000-00500000 Deferred game.exe",
            "PE 7bc00000-7bd00000 Export ntdll.dll",
        ]
    )
    assert result.module == "ntdll.dll"


@pytest.mark.parametrize(
    "address, expected",
    [
        ("00400000", "game.exe"),
        ("004fffff", "game.exe"),
        ("00500000", ""),
        ("003fffff", ""),
        ("deadbeef", ""),
    ],
)
def test_module_ranges_are_half_open(address, expected):
    result = watch(
        [
            f"Unhandled exception 0xc0000005 at address {address}",
            "PE 00400000-00500000 Deferred game.exe",
        ]
    )
    assert result.detected
    assert result.module == expected


@pytest.mark.parametrize(
    "line",
    [
        "wine: ordinary output at address 00401234",
        "example: Unhandled exception at address 00401234",
        "Unhandled exception without an instruction address",
        "=>0 0x00401234 in game (+0x1234)",
    ],
)
def test_noise_does_not_report_crash(line):
    result = watch([*MODULES, line])
    assert not result.detected
    assert result.module == ""


def test_parser_storage_is_bounded():
    from nadare.core.winecrash import WineCrash

    result = WineCrash()
    for i in range(result.MAX_MODULES + 100):
        result.feed(f"PE {i * 4096:08x}-{(i + 1) * 4096:08x} Deferred m{i}.dll")
    assert len(result._modules) == result.MAX_MODULES
    before = list(result._modules)
    result.feed("PE 00400000-00500000 Deferred " + "x" * result.MAX_LINE)
    assert list(result._modules) == before


@pytest.fixture
def view():
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameView

    app = QApplication.instance() or QApplication([])
    page = GameView()
    page.set_profile("g", {"backend": "wine", "title": "G"})
    page.shown = []
    page._failure_choice = lambda title, message, retry, **kw: (
        page.shown.append((title, message, retry, kw)) or "close"
    )
    yield page
    page.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("seconds", [2, 600])
@pytest.mark.parametrize("known", [False, True])
def test_failure_box_names_wine_module_and_runner(view, seconds, known):
    view.set_busy("launch")
    view.append_event(
        Event(
            "log",
            "runner",
            data={
                "runner": "GE-Proton10-3",
                "runner_recommended": False,
                "runner_default": "DW-Proton",
            },
        )
    )
    for line in [CRASH, *(MODULES if known else [])]:
        view.append_event(Event("log", mask(line)))
    view._last_step_at = time.time() - seconds
    view.finish("launch", 0xC0000005)
    title, message, retry, options = view.shown[0]
    assert title == "The game crashed"
    expected = (
        "The game crashed inside unityplayer.dll (Wine)" if known else "The game crashed (Wine)"
    )
    assert expected in message
    assert "exit code 3221225477" in message
    assert "GE-Proton10-3" in message and "DW-Proton" in message
    assert retry and options == {"settings": True}


@pytest.mark.parametrize("ending", ["stop", "success", "next_launch", "install"])
def test_crash_state_does_not_leak_or_override_stop(view, ending):
    view.set_busy("launch")
    view.append_event(Event("log", CRASH))
    if ending == "next_launch":
        view.set_busy("launch")
    elif ending == "install":
        view.set_busy("install")
    elif ending == "stop":
        view._busy_op = "stop"
    view.finish("install" if ending == "install" else "launch", 0 if ending == "success" else 5)
    if ending in {"stop", "success"}:
        assert view.shown == []
    else:
        assert view.shown[0][0] == "Operation failed"


def test_elf_module_is_resolved():
    result = watch([CRASH.replace("00006FFFFFC174D7", "00007FFFF7001234"), *MODULES])
    assert result.module == "libwine.so.1"


def test_stream_assembles_crash_line_written_in_pieces(view):
    import sys

    from nadare.backends.wine import stream

    view.set_busy("launch")
    # Exercise the actual reader, without Wine or network access. The final module
    # line also has no newline, so the drain at EOF must deliver it.
    script = (
        "import sys, time\n"
        f"sys.stdout.write({CRASH[:40]!r}); sys.stdout.flush()\n"
        "time.sleep(0.03)\n"
        f"sys.stdout.write({CRASH[40:] + chr(10)!r}); sys.stdout.flush()\n"
        f"sys.stdout.write({chr(10).join(MODULES)!r})\n"
        "sys.exit(5)\n"
    )
    code = stream(
        [sys.executable, "-c", script],
        {},
        on_line=lambda line: view.append_event(Event("log", mask(line))),
    )
    view.finish("launch", code)
    assert "The game crashed inside unityplayer.dll (Wine), exit code 5." in view.shown[0][1]
