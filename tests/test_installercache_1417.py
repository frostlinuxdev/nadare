# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1417 phase 2b: installers are kept only while a library game needs them.

File names are the ones measured on the test desktop and flxpc (2026-11-16).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nadare.core import installercache as ic
from nadare.core import paths

DAY = 86400
WV2 = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
VC = "vc_redist.x64.exe"


def _file(rel: str, age: float = 2 * DAY, size: int = 100) -> Path:
    path = paths.cache_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def _p(backend: str, **extra: object) -> dict:
    return {"backend": backend, **extra}


# ---- which folder a game needs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "folder"),
    [
        (_p("stove-czn"), "czn"),
        (_p("direct-gfl2-haoplay"), "czn"),
        (_p("steam", app_id="5019180"), "czn"),  # Epic Seven: WebView2 fix
        (_p("steam", extra={"app_id": "3308670"}), "czn"),  # GFL2 Haoplay on Steam
        (_p("wine"), "wine"),
        (_p("arknights-yostar"), "arknights-yostar"),
        (_p("stellasora-yostar"), "arknights-yostar"),
        (_p("launcher"), "arknights-yostar"),
        (_p("launcher-nikke"), "arknights-yostar"),
    ],
)
def test_the_folder_each_game_needs(profile: dict, folder: str) -> None:
    assert folder in ic.needed_folders({"g": profile})


@pytest.mark.parametrize(
    "profile",
    [_p("direct-wuwa"), _p("direct-gfl2"), _p("steam", app_id="123"), _p("steam"), {}],
)
def test_games_without_installers_need_none(profile: dict) -> None:
    assert ic.needed_folders({"g": profile}) == set()


# ---- what is removed -------------------------------------------------------------------------


def test_installers_of_games_no_longer_in_the_library_go() -> None:
    wv2 = _file(f"czn/{WV2}", size=215)
    czn_vc = _file(f"czn/{VC}", size=25)
    ark_vc = _file(f"arknights-yostar/{VC}", size=25)
    nikke = _file("arknights-yostar/nikkeminiloader0.0.6.380.exe", size=9)
    result = ic.sweep({"w": _p("direct-wuwa")})
    assert not wv2.exists() and not czn_vc.exists() and not ark_vc.exists() and not nikke.exists()
    assert result.freed == 215 + 25 + 25 + 9


def test_installers_of_library_games_stay() -> None:
    keep = [
        _file(f"czn/{WV2}"),
        _file(f"czn/{VC}"),
        _file(f"arknights-yostar/{VC}"),
        _file("arknights-yostar/Arknights_EN_Gamelauncher-setup.exe"),
        _file(f"wine/{VC}"),
    ]
    profiles = {"c": _p("stove-czn"), "a": _p("arknights-yostar"), "x": _p("wine")}
    assert ic.sweep(profiles).removed == []
    assert all(p.exists() for p in keep)


def test_only_the_newest_version_of_a_launcher_installer_stays() -> None:
    old = _file("arknights-yostar/nikkeminiloader0.0.6.346.exe", age=10 * DAY)
    new = _file("arknights-yostar/nikkeminiloader0.0.6.380.exe", age=2 * DAY)
    vc = _file(f"arknights-yostar/{VC}", age=30 * DAY)
    ic.sweep({"n": _p("launcher-nikke")})
    assert not old.exists()
    assert new.exists() and vc.exists()


def test_a_fresh_older_version_is_kept_for_a_day() -> None:
    # A download in progress or just finished may be the one in use; only old ones go.
    old = _file("arknights-yostar/nikkeminiloader0.0.6.346.exe", age=3600)
    _file("arknights-yostar/nikkeminiloader0.0.6.380.exe", age=60)
    ic.sweep({"n": _p("launcher-nikke")})
    assert old.exists()


def test_young_installers_of_removed_games_stay_for_a_day() -> None:
    wv2 = _file(f"czn/{WV2}", age=3600)
    ic.sweep({})
    assert wv2.exists(), "it may be downloading for a game being added right now"


def test_only_installer_files_are_touched() -> None:
    keep = [
        _file("czn/notes.json"),
        _file("czn/.MicrosoftEdgeWebView2RuntimeInstallerX64.exe.lock"),
        _file("arknights-yostar/manifest.json"),
        _file("nikke-latest/NIKKE.json"),
        _file("runners/dwproton-11.0-17-x86_64.tar.xz"),
        _file(f"umu/{VC}"),  # a folder the table does not own
        _file(VC),  # the cache root itself
    ]
    ic.sweep({})
    assert all(p.exists() for p in keep)


def test_links_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / VC
    target.write_bytes(b"precious")
    t = time.time() - 2 * DAY
    os.utime(target, (t, t))
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    (paths.cache_dir() / "wine").symlink_to(outside)
    (paths.cache_dir() / "czn").mkdir()
    link = paths.cache_dir() / "czn" / WV2
    link.symlink_to(target)
    ic.sweep({})
    assert target.read_bytes() == b"precious"


def test_errors_never_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    _file(f"czn/{WV2}")

    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError("ro")

    monkeypatch.setattr(ic.os, "unlink", boom)
    assert ic.sweep({}).removed == []


def test_a_broken_profile_entry_is_skipped_not_fatal() -> None:
    wv2 = _file(f"czn/{WV2}")
    vc = _file(f"wine/{VC}")
    ic.sweep({"bad": "not a dict", "x": _p("wine")})  # type: ignore[dict-item]
    assert not wv2.exists(), "the rest of the library is still judged"
    assert vc.exists()


def test_hidden_files_are_not_installers() -> None:
    """A WebView2 run copy belongs to the leftover sweep and its owner-pid rule."""
    run = _file(f"czn/.webview2-run-{os.getpid()}-{'a' * 32}.exe")
    ic.sweep({})
    assert run.exists()


def test_one_failed_removal_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [_file(f"czn/{WV2}"), _file(f"czn/{VC}"), _file(f"wine/{VC}")]
    real_unlink = os.unlink
    failed: list[str] = []

    def first_fails(path: object, *a: object, **k: object) -> None:
        if not failed:
            failed.append(str(path))
            raise PermissionError("busy")
        real_unlink(path, *a, **k)

    monkeypatch.setattr(ic.os, "unlink", first_fails)
    result = ic.sweep({})
    assert [str(p) for p in files if p.exists()] == failed
    assert len(result.removed) == 2


# ---- GUI wiring ------------------------------------------------------------------------------


class _Cfg:
    def __init__(self, profiles: dict, notice: str | None = None) -> None:
        self.profiles = profiles
        self.notice = notice
        self.data: dict = {}


class _Win:
    from nadare.gui.main_window_parts.cleanup import CleanupMixin as _M

    _start_leftover_sweep = _M._start_leftover_sweep
    _run_leftover_sweep = _M._run_leftover_sweep

    def __init__(self, config: _Cfg) -> None:
        self.config = config


def _sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    class T:
        def __init__(self, target, args=(), **_k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", T)


def test_start_up_drops_unneeded_installers(monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_threads(monkeypatch)
    wv2 = _file(f"czn/{WV2}")
    _Win(_Cfg({"w": _p("direct-wuwa")}))._start_leftover_sweep()
    assert not wv2.exists()


def test_start_up_keeps_installers_when_the_config_is_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_threads(monkeypatch)
    wv2 = _file(f"czn/{WV2}")
    _Win(_Cfg({}, notice="recovered"))._start_leftover_sweep()
    assert wv2.exists()


def test_the_profiles_are_copied_before_the_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: list = []
    me = threading.get_ident()

    def fake_sweep(profiles):
        # Count only this test's call: a leftover-sweep thread from an earlier GUI test's 5 s
        # start-up timer can outlive conftest's bounded join and land here (CI 3.13, #1641).
        if threading.get_ident() == me:
            seen.append(profiles)
        return ic.Result()

    monkeypatch.setattr(ic, "sweep", fake_sweep)
    _sync_threads(monkeypatch)
    profiles = {"c": _p("stove-czn")}
    _Win(_Cfg(profiles))._start_leftover_sweep()
    assert seen == [profiles] and seen[0] is not profiles
