# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the running-game detector.

A shell/editor/file manager that merely mentions the prefix path must never
read as "running": only Wine/Proton/umu processes for the profile's prefix, or
a process whose ``argv[0]`` is the game/launcher exe, count.  ``scan_pids``
takes a fake ``/proc`` root so nothing here inspects the real machine.
"""

from __future__ import annotations

from pathlib import Path

from nadare.backends.wine import ProcessQuery, scan_pids
from nadare.core.process import ProcessScanRound, use_process_scan_round


def _pid(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is not None:
        blob = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
        (entry / "environ").write_bytes(blob)


def _prefix_scan(root: Path, prefix: str, names: tuple[str, ...] = ("game.exe",)) -> list[int]:
    return scan_pids(ProcessQuery(prefix=prefix, names=names), proc_root=root)


def test_prefix_env_matches_only_this_prefix(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 100, ["/usr/bin/wine", "/games/game.exe"], {"WINEPREFIX": "/games/pfx"})
    _pid(root, 101, ["/usr/bin/wine", "/games/game.exe"], {"WINEPREFIX": "/other/pfx"})
    _pid(root, 102, ["/usr/bin/umu-run"], {"STEAM_COMPAT_DATA_PATH": "/games/pfx"})

    assert sorted(_prefix_scan(root, "/games/pfx")) == [100, 102]


def test_a_shell_mentioning_the_prefix_is_not_running(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    for pid in (200, 201, 202):
        _pid(root, pid, ["/usr/bin/bash", "-c", "cd /games/pfx && ls"])
    _pid(root, 203, ["/usr/bin/gedit", "/games/game.exe"])
    _pid(root, 204, ["/usr/bin/dolphin", "/games/pfx"])

    assert _prefix_scan(root, "/games/pfx") == []


def test_exe_name_on_argv0_matches(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 300, ["/games/pfx/drive_c/game.exe", "--fullscreen"])
    # A different exe that merely *contains* the name must not match.
    _pid(root, 301, ["/games/notgame.exe"])
    _pid(root, 302, ["/usr/bin/wine", "/games/game.exe"])  # argv[0] is wine

    assert _prefix_scan(root, "/games/pfx") == [300]


def test_explicit_process_match_is_a_substring_override(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 400, ["/usr/bin/mygame", "--server"])
    _pid(root, 401, ["/usr/bin/other"])

    query = ProcessQuery(prefix="/games/pfx", names=("game.exe",), match="mygame")
    assert scan_pids(query, proc_root=root) == [400]


def test_empty_query_matches_nothing(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 500, ["/usr/bin/wine"], {"WINEPREFIX": "/games/pfx"})
    assert scan_pids(ProcessQuery(), proc_root=root) == []


def test_legacy_substring_patterns_still_work(tmp_path: Path) -> None:
    """The generic Steam/script backends keep their substring behaviour."""
    root = tmp_path / "proc"
    _pid(root, 600, ["/usr/bin/steam", "steam://rungameid/730"])
    assert scan_pids(["730"], proc_root=root) == [600]


def test_missing_proc_root_is_safe(tmp_path: Path) -> None:
    query = ProcessQuery(prefix="/x", names=("a.exe",))
    assert scan_pids(query, proc_root=tmp_path / "nope") == []


def test_prefix_and_names_pair_form(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 700, ["/usr/bin/wine"], {"WINEPREFIX": "/games/pfx"})
    assert scan_pids("/games/pfx", ["game.exe"], proc_root=root) == [700]


def test_explicit_probe_round_reuses_one_snapshot_but_independent_scan_is_fresh(tmp_path):
    root = tmp_path / "proc"
    _pid(root, 800, ["/usr/bin/wine", "game.exe"], {"WINEPREFIX": "/games/one"})
    round_ = ProcessScanRound(proc_root=root)

    with use_process_scan_round(round_):
        assert scan_pids(ProcessQuery(prefix="/games/one"), proc_root=root) == [800]
        (root / "800").rename(root / "801")
        assert scan_pids(ProcessQuery(prefix="/games/one"), proc_root=root) == [800]
        assert scan_pids(ProcessQuery(prefix="/games/two"), proc_root=root) == []

    assert scan_pids(ProcessQuery(prefix="/games/one"), proc_root=root) == [801]


def test_status_worker_uses_the_probe_round_payload(tmp_path, monkeypatch):
    from nadare.core.backends import Status
    from nadare.core.config import Config
    from nadare.gui import workers

    root = tmp_path / "proc"
    _pid(root, 810, ["/usr/bin/wine", "game.exe"], {"WINEPREFIX": "/games/one"})
    round_ = ProcessScanRound(proc_root=root)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine")
    result = []

    class FakeBackend:
        def status(self):
            query = ProcessQuery(prefix="/games/one")
            first = scan_pids(query, proc_root=root)
            (root / "810").rename(root / "811")
            second = scan_pids(query, proc_root=root)
            return Status("installed", data={"pids": (first, second)})

    monkeypatch.setattr(workers, "create", lambda *args, **kwargs: FakeBackend())
    worker = workers.BackendWorker(config, "p", "status", payload=round_)
    worker.result.connect(result.append)
    worker.run()

    assert result[0].data["pids"] == ([810], [810])


# ---- Proton / umu: the Wine processes carry WINEPREFIX=<prefix>/pfx and the data path ----------
def test_proton_wine_processes_match_through_the_compat_data_path(tmp_path: Path) -> None:
    """Seen on a real umu launch: Wine's own processes have WINEPREFIX=<prefix>/pfx/ and
    STEAM_COMPAT_DATA_PATH=<prefix>. The renderer probe looked only at the python launchers."""
    root = tmp_path / "proc"
    pfx = {"WINEPREFIX": "/games/pfx/pfx/", "STEAM_COMPAT_DATA_PATH": "/games/pfx"}
    _pid(root, 500, ["python3", "umu-run", "/games/game.exe"],
         {"WINEPREFIX": "/games/pfx", "STEAM_COMPAT_DATA_PATH": "/games/pfx"})
    _pid(root, 501, ["c:\\windows\\system32\\services.exe"], pfx)
    _pid(root, 502, ["X:\\games\\game.exe"], pfx)
    _pid(root, 503, ["C:\\windows\\system32\\explorer.exe"], pfx)

    assert sorted(_prefix_scan(root, "/games/pfx")) == [500, 501, 502, 503]


def test_a_proton_process_of_another_prefix_still_does_not_match(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    other = {"WINEPREFIX": "/other/pfx/pfx/", "STEAM_COMPAT_DATA_PATH": "/other/pfx"}
    _pid(root, 510, ["X:\\games\\game.exe"], other)  # same exe name, different prefix

    assert _prefix_scan(root, "/games/pfx") == []


def test_a_proton_prefix_reached_only_through_wineprefix_pfx_matches(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    _pid(root, 520, ["X:\\games\\game.exe"], {"WINEPREFIX": "/games/pfx/pfx/"})

    assert _prefix_scan(root, "/games/pfx") == [520]


def test_the_renderer_probe_sees_the_wine_game_of_a_proton_launch(tmp_path: Path) -> None:
    """The scan must reach the game process, or its DXVK libraries never get classified."""
    from nadare.core import graphics

    root = tmp_path / "proc"
    _pid(root, 600, ["python3", "umu-run", "/games/game.exe"],
         {"WINEPREFIX": "/games/pfx", "STEAM_COMPAT_DATA_PATH": "/games/pfx"})
    (root / "600" / "maps").write_text("7f00 r-xp /usr/lib/libGL.so.1\n")
    _pid(root, 601, ["X:\\games\\game.exe"],
         {"WINEPREFIX": "/games/pfx/pfx/", "STEAM_COMPAT_DATA_PATH": "/games/pfx"})
    (root / "601" / "maps").write_text(
        "7f10 r-xp /games/pfx/pfx/drive_c/windows/system32/d3d11.dll\n"
        "7f20 r-xp /games/pfx/pfx/drive_c/windows/system32/dxgi.dll\n"
        "7f30 r-xp /usr/lib/x86_64-linux-gnu/libvulkan.so.1\n"
    )
    pids = _prefix_scan(root, "/games/pfx")

    assert graphics.detect_renderer(pids, proc_root=root) == "DXVK on Vulkan"


def test_a_process_name_that_is_not_utf8_does_not_break_the_scan(tmp_path):
    """#928: the kernel cuts comm at 15 bytes, which can split a multi-byte character."""
    from nadare.core import process

    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    broken = "ゲーム".encode()[:-1]  # a truncated character
    (proc / "comm").write_bytes(broken + b"\n")
    fields = " ".join(["S"] + ["0"] * 18 + ["777"] + ["0"] * 5)
    (proc / "stat").write_bytes(b"4242 (" + broken + b") " + fields.encode() + b"\n")
    (proc / "cmdline").write_bytes(b"game\x00")

    assert process._pid_starttime(4242, root=tmp_path / "proc") == "777"
    _cmdline, _exe, name = process._read_cmdline(4242, root=tmp_path / "proc")
    assert name.startswith("ゲー")
