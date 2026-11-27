# SPDX-License-Identifier: GPL-3.0-or-later
"""The 2026-11-26 launch-sheet findings, each with its guard (#1821 follow-up).

SEC-launch 1-4, NEON 4/9/10 and SIM-launch F1-F3: a seen first frame must not be
re-scanned per later line, the detector and the log cap share one prefix grammar,
the presenter line survives the info cap, ``events.mask`` stays linear on one huge
token, a failed ``game_pids()`` read keeps the watch polling, and ``process_match``
only opens the first-frame gate for the game itself.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import wine
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.direct import DirectBackend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.script import ScriptBackend
from nadare.backends.steam import SteamBackend
from nadare.backends.wine import ProcessQuery, WineBackend
from nadare.core import launch_window
from nadare.core.config import Config
from nadare.core.events import Cancel, mask
from nadare.gui import workers

PRESENTER = "info:  Presenter: Actual swapchain properties:"
DECIMAL_PRESENTER = "1234.567:info:  Presenter: Actual swapchain properties:"
DECIMAL_VKD3D = "0123.456:0124:info: Creating swapchain (1280 x 720), BufferCount = 3."


def _wine_profile(tmp_path, *, exe="game.exe", profile=None):
    game = tmp_path / "game"
    write_exe(game / exe)
    values = {
        "install_dir": str(game),
        "exe_name": exe,
        "prefix": str(tmp_path / "prefix"),
        "wine_binary": "/usr/bin/wine",
    }
    values.update(profile or {})
    return WineBackend(make_context(tmp_path, "wine", values))


def _proc_entry(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    if env is not None:
        (entry / "environ").write_bytes(
            b"".join(f"{key}={value}".encode() + b"\0" for key, value in env.items())
        )


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """A fake /proc for the real ``scan_pids``: no scan is stubbed out, only its root."""
    root = tmp_path / "proc"
    root.mkdir()
    real_scan = wine.scan_pids
    monkeypatch.setattr(wine, "_own_pid", lambda: -1)
    monkeypatch.setattr(wine, "scan_pids", lambda query: real_scan(query, proc_root=root))
    return root


# ---- 1. a seen first frame is not re-scanned -------------------------------
def test_feed_does_not_scan_processes_after_the_first_frame(tmp_path):
    calls = []
    probe = launch_window.WindowEvidence(
        set(),
        proc_root=tmp_path / "gone",
        game_processes=lambda: calls.append(True) or True,
    )
    probe.feed(PRESENTER)
    assert calls == [True]
    for _ in range(100):
        probe.feed(PRESENTER)
    assert calls == [True]


# ---- 2. one shared prefix grammar for the detector and the cap ------------
@pytest.mark.parametrize("line", [DECIMAL_PRESENTER, DECIMAL_VKD3D])
def test_a_decimal_timestamp_presenter_line_is_the_first_frame(tmp_path, line):
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    probe.feed(line)
    assert probe.seen([], 0.0, {})


def test_the_detector_and_the_info_cap_share_one_prefix_grammar():
    accepted = [
        PRESENTER,
        "0124:" + PRESENTER,
        "0008:0124:" + PRESENTER,
        DECIMAL_PRESENTER,
        DECIMAL_VKD3D,
    ]
    for line in accepted:
        assert launch_window._FIRST_FRAME_RE.match(line), line
        assert workers._LAYER_INFO_RE.match(line), line
    for line in ("INFO:  Presenter: Actual swapchain properties:", "\x00" + PRESENTER):
        assert not launch_window._FIRST_FRAME_RE.match(line), line
        assert not workers._LAYER_INFO_RE.match(line), line


# ---- 3. the presenter line survives the 20-line info cap ------------------
def test_the_presenter_line_stays_in_the_run_log_after_the_cap(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config(), "g", "launch")
    for index in range(workers.LAYER_INFO_LINES + 5):
        worker._on_event(workers.Event("log", f"info:  DXVK: noise {index}"))
    worker._on_event(workers.Event("log", PRESENTER))
    worker._close_log()

    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    assert PRESENTER in text
    assert text.count("DXVK: noise") == workers.LAYER_INFO_LINES


def test_a_later_presenter_line_is_kept_without_spending_the_cap(tmp_path, monkeypatch):
    """A decimal/hex presenter after the cap is kept too, and does not eat a noise slot."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config(), "g", "launch")
    for index in range(workers.LAYER_INFO_LINES + 3):
        worker._on_event(workers.Event("log", f"info:  DXVK: noise {index}"))
    worker._on_event(workers.Event("log", DECIMAL_PRESENTER))
    worker._close_log()

    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    assert DECIMAL_PRESENTER in text
    assert text.count("DXVK: noise") == workers.LAYER_INFO_LINES


# ---- 4. events.mask is linear on one long token ---------------------------
def test_masking_a_20kb_single_token_is_linear():
    # Linear costs ~10 ms here; the quadratic bug took seconds. Best of three with a 0.5 s
    # bound tells them apart without failing on a loaded CI runner (70 ms seen, 2026-11-26).
    text = "A" * 20_000
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        masked = mask(text)
        timings.append(time.perf_counter() - started)
    assert "AAAA…AAAA<20000>" == masked
    assert min(timings) < 0.5, f"mask took {min(timings) * 1000:.0f} ms"


def test_an_underscore_grouped_hex_token_is_still_masked():
    # Readability of long snake_case symbols must not open a hole for grouped tokens.
    secret = "ab12cd34_ef56ab78_90cd12ef_3456abcd"
    assert secret not in mask(secret)


def test_a_long_hex_token_is_still_masked():
    secret = "0123456789abcdef" * 20
    assert secret not in mask(secret)


def test_a_short_snake_case_token_is_still_masked():
    secret = "ab_cd_1234567"
    assert secret not in mask(secret)


# ---- 5. a failed process read keeps the watch alive -----------------------
def test_a_failing_process_read_does_not_end_the_watch(monkeypatch, caplog):
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.005)

    def pids():
        raise OSError("simulated /proc failure")

    emitted = []
    caplog.set_level(logging.WARNING, logger="nadare.core.launch_window")
    with launch_window.watch(pids, emitted.append, Cancel()) as watched:
        time.sleep(0.05)
        watched.feed(PRESENTER)
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]
    warnings = [record for record in caplog.records if record.name == "nadare.core.launch_window"]
    assert len(warnings) == 1


# ---- 6. process_match names the game, inside the prefix, argv[0] only ----
def test_process_match_outside_the_prefix_does_not_open_the_gate(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path, exe="Stub.exe", profile={"process_match": "RealGame.exe"})
    _proc_entry(fake_proc, 700, [r"Z:\games\RealGame.exe"])
    assert wine.game_processes(backend)() is False


def test_process_match_does_not_match_the_arguments_of_another_exe(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path, exe="Stub.exe", profile={"process_match": "RealGame.exe"})
    prefix = backend._patterns().prefix
    _proc_entry(
        fake_proc,
        701,
        [r"Z:\games\Other.exe", r"Z:\games\RealGame.exe"],
        {"WINEPREFIX": prefix},
    )
    assert wine.game_processes(backend)() is False


def test_process_match_on_argv0_inside_the_prefix_still_opens_the_gate(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path, exe="Stub.exe", profile={"process_match": "RealGame.exe"})
    prefix = backend._patterns().prefix
    _proc_entry(fake_proc, 702, [r"Z:\games\RealGame.exe"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend)() is True


def test_launcher_names_and_exclusions_ignore_case(tmp_path, fake_proc):
    prefix = str(tmp_path / "prefix")

    def backend():
        return SimpleNamespace(
            _patterns=lambda: ProcessQuery(
                prefix=prefix, names=("Launcher.EXE", "Helper.EXE", "Game.exe")
            ),
            _launcher_query=lambda: ProcessQuery(names=("launcher.exe",)),
            game_process_exclude=("helper.exe",),
            ctx=SimpleNamespace(profile={}),
        )

    _proc_entry(fake_proc, 703, [r"Z:\x\Launcher.EXE"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend())() is False
    _proc_entry(fake_proc, 704, [r"Z:\x\Helper.EXE"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend())() is False
    _proc_entry(fake_proc, 705, [r"Z:\x\Game.exe"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend())() is True


@pytest.mark.parametrize("kind", ["wine", "direct", "arknights"])
def test_every_wine_family_backend_with_process_match_finds_its_game(kind, tmp_path, fake_proc):
    install = tmp_path / kind
    write_exe(install / "Stub.exe")
    values = {
        "install_dir": str(install),
        "exe_name": "Stub.exe",
        "prefix": str(tmp_path / "prefix"),
        "process_match": "RealGame.exe",
    }
    classes = {
        "wine": (WineBackend, "wine"),
        "direct": (DirectBackend, "direct"),
        "arknights": (ArknightsBackend, "arknights-yostar"),
    }
    cls, context_kind = classes[kind]
    backend = cls(make_context(tmp_path, context_kind, values))
    prefix = str(backend._patterns().prefix)
    _proc_entry(fake_proc, 800, [r"Z:\games\RealGame.exe"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend)() is True


def test_steam_and_script_process_match_still_find_their_game(tmp_path):
    write_exe(tmp_path / "run.sh")
    steam = SteamBackend(
        make_context(
            tmp_path,
            "steam",
            {"steam_app_id": "42", "process_match": "RealGame.exe"},
        )
    )
    script = ScriptBackend(
        make_context(
            tmp_path,
            "script",
            {
                "launch_exe": "run.sh",
                "install_dir": str(tmp_path),
                "process_match": "RealGame.exe",
            },
        )
    )
    root = tmp_path / "proc"
    root.mkdir()
    _proc_entry(root, 810, ["/usr/bin/wine", r"Z:\games\RealGame.exe"])
    assert wine.scan_pids(steam._patterns("42"), proc_root=root) == [810]
    assert wine.scan_pids(script._patterns(), proc_root=root) == [810]


def test_the_nikke_stop_rule_still_follows_process_match(tmp_path):
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"prefix": str(tmp_path / "prefix"), "process_match": "CustomGame.exe"},
        )
    )
    root = tmp_path / "proc"
    root.mkdir()
    _proc_entry(root, 820, [r"Z:\games\CustomGame.exe"])
    query = backend._patterns()
    assert query.match == "CustomGame.exe"
    assert wine.scan_pids(query, proc_root=root) == [820]


def test_an_exact_exe_name_without_prefix_env_still_opens_the_gate(tmp_path, fake_proc):
    # A process whose environment carries no WINEPREFIX/STEAM_COMPAT_DATA_PATH (a child a
    # publisher launcher spawned with its own block) is still the game on an exact exe
    # name, as before; only the looser process_match needs the prefix.
    backend = _wine_profile(tmp_path, exe="Game.exe")
    _proc_entry(fake_proc, 706, [r"Z:\games\Game.exe"], {"HOME": "/home/u"})
    assert wine.game_processes(backend)() is True


def test_resize_reallocations_after_the_cap_do_not_flood_the_log(tmp_path, monkeypatch):
    """Presenter lines have their own small budget: DXVK reallocates on every resize."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config(), "g", "launch")
    for index in range(workers.LAYER_INFO_LINES + 3):
        worker._on_event(workers.Event("log", f"info:  DXVK: noise {index}"))
    for size in range(200):
        line = f"info:Reallocating swapchain ({1000 + size} x 600), BufferCount = 3"
        worker._on_event(workers.Event("log", line))
    worker._close_log()

    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    assert text.count("Reallocating swapchain") == workers.PRESENTER_LINES


def test_process_match_ignores_arguments_inside_a_single_string_argv0(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path, exe="Stub.exe", profile={"process_match": "RealGame.exe"})
    prefix = backend._patterns().prefix
    argv = [r"Z:\games\Stub.exe Z:\games\RealGame.exe"]
    _proc_entry(fake_proc, 710, argv, {"WINEPREFIX": prefix})
    assert wine.game_processes(backend)() is False


def test_process_match_ignores_case(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path, exe="Stub.exe", profile={"process_match": "RealGame.exe"})
    prefix = backend._patterns().prefix
    _proc_entry(fake_proc, 711, [r"Z:\GAMES\REALGAME.EXE"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend)() is True


def test_a_three_token_wine_prefix_is_capped_and_is_a_frame():
    noise = "1234.567:0100:0124:info:  DXVK: noise"
    frame = "1234.567:0100:0124:info:  Presenter: Actual swapchain properties:"
    assert workers._LAYER_INFO_RE.match(noise)
    assert launch_window.is_first_frame(frame)


def test_a_launcher_presenter_does_not_push_the_games_out_of_the_log(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config(), "g", "launch")
    worker._on_event(workers.Event("log", "info:Creating swapchain (800 x 600), BufferCount = 2"))
    for index in range(workers.LAYER_INFO_LINES + 3):
        worker._on_event(workers.Event("log", f"info:  DXVK: noise {index}"))
    worker._on_event(workers.Event("log", PRESENTER))
    worker._close_log()
    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    assert "(800 x 600)" in text and PRESENTER in text
    assert text.count("DXVK: noise") == workers.LAYER_INFO_LINES


@pytest.mark.parametrize(
    "line",
    [
        "--" + "token" * 4000,
        ("a" * 35 + " ") * 555,
        "A" * 20_000,
        "x " * 10_000,
    ],
)
def test_a_huge_game_line_is_cut_and_masked_quickly(line):
    started = time.perf_counter()
    text = workers.mask_log_line(line)
    assert time.perf_counter() - started < 0.1
    assert len(text) <= workers.LOG_LINE_CHARS + 40


def test_a_token_crossing_the_cut_is_dropped_whole():
    secret = "Zq9" * 40
    line = "x" * (workers.LOG_LINE_CHARS - 10) + " " + secret
    text = workers.mask_log_line(line)
    assert "Zq9" not in text
    assert "chars cut" in text


def test_a_long_sessid_key_name_is_still_a_secret_key():
    from nadare.core.events import is_secret_key

    assert is_secret_key("a" * 40 + "sessid")
