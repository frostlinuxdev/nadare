# SPDX-License-Identifier: GPL-3.0-or-later
"""#1425: a pin bump must not put a runner download inside a Play click.

After an update bumps the tested DW-Proton pin, the first Play used to download ~326 MiB before
the game started. Nadare now fetches the new pin in the background at start-up when a game will
need it and an older build of that family is already installed (that is what a pin bump looks
like). Fresh setups keep fetching at install time, as before; nothing is fetched when the user
turned update checks off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import runners

PIN = runners.pinned_tag("dwproton")


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


def _make(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    return d


OLD = "dwproton-11.0-14-x86_64"


def test_a_pin_bump_is_fetched_for_a_game_on_the_default_runner(root):
    _make(root, OLD)
    assert runners.prefetch_families({"g": {"backend": "wine"}}) == ["dwproton"]


@pytest.mark.parametrize("word", ["wine", "system", "none", "", "dwproton"])
def test_words_that_mean_the_default_runner_count(root, word):
    _make(root, OLD)
    profile = {"backend": "wine", "runner": word}
    assert runners.prefetch_families({"g": profile}) == ["dwproton"]


def test_nothing_when_the_pin_is_already_installed(root):
    _make(root, OLD)
    _make(root, f"{PIN}-x86_64")
    assert runners.prefetch_families({"g": {"backend": "wine"}}) == []


def test_nothing_on_a_fresh_setup_with_no_build_of_the_family(root):
    # Not a pin bump: the install fetches it, as before (no surprise download at start-up).
    assert runners.prefetch_families({"g": {"backend": "wine"}}) == []


def test_nothing_when_no_game_uses_the_family(root):
    _make(root, OLD)
    ge = _make(root, "GE-Proton9-1")
    assert runners.prefetch_families({"g": {"backend": "wine", "runner": str(ge)}}) == []
    assert runners.prefetch_families({}) == []


def test_an_own_wine_binary_is_not_the_default_runner(root):
    _make(root, OLD)
    profile = {"backend": "wine", "wine_binary": "/usr/bin/wine"}
    assert runners.prefetch_families({"g": profile}) == []
    profile = {"backend": "wine", "extra": {"wine_binary": "/usr/bin/wine"}}
    assert runners.prefetch_families({"g": profile}) == []


def test_a_backend_without_the_default_runner_is_ignored(root):
    _make(root, OLD)
    assert runners.prefetch_families({"g": {"backend": "steam"}}) == []


def test_a_family_named_under_extra_counts(root):
    _make(root, OLD)
    profile = {"backend": "steam", "extra": {"runner": "dwproton"}}
    assert runners.prefetch_families({"g": profile}) == ["dwproton"]


def test_each_family_once(root):
    _make(root, OLD)
    profiles = {f"g{i}": {"backend": "wine"} for i in range(4)}
    assert runners.prefetch_families(profiles) == ["dwproton"]


def test_prefetch_downloads_each_family_and_reports_what_is_new(root, monkeypatch):
    _make(root, OLD)
    calls = []

    def fake_install(family, *, latest=False, emit=None, cancel=None):
        calls.append((family, latest))
        return _make(root, f"{PIN}-x86_64")

    monkeypatch.setattr(runners, "install", fake_install)
    got = runners.prefetch({"g": {"backend": "wine"}, "h": {"backend": "wine"}})
    assert calls == [("dwproton", False)]
    assert got == [root / f"{PIN}-x86_64"]
    # A second run finds it installed and downloads nothing.
    assert runners.prefetch({"g": {"backend": "wine"}}) == []
    assert len(calls) == 1


def test_a_failed_prefetch_is_not_fatal(root, monkeypatch):
    _make(root, OLD)

    def boom(*a, **k):
        raise runners.RunnerError("offline")

    monkeypatch.setattr(runners, "install", boom)
    assert runners.prefetch({"g": {"backend": "wine"}}) == []


# ---- GUI wiring -----------------------------------------------------------------------------
class _Cfg:
    def __init__(self, data, profiles, notice=None):
        self.data = data
        self.profiles = profiles
        self.notice = notice


class _Win:
    from nadare.gui.main_window_parts.cleanup import CleanupMixin as _M

    _start_runner_prefetch = _M._start_runner_prefetch
    _run_runner_prefetch = _M._run_runner_prefetch

    def __init__(self, config):
        self.config = config
        self.measured = []

    def _measure_stale(self, profiles):
        self.measured.append(profiles)


def _sync_threads(monkeypatch):
    import threading

    class T:
        def __init__(self, target, args=(), **_k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", T)


def test_gui_prefetch_then_offers_the_old_runners(monkeypatch):
    _sync_threads(monkeypatch)
    seen = []
    monkeypatch.setattr(runners, "prefetch", lambda p, **k: seen.append(p) or [Path("/new")])
    win = _Win(_Cfg({"check_updates": True}, {"g": {"backend": "wine"}}))
    win._start_runner_prefetch()
    assert seen == [{"g": {"backend": "wine"}}]
    assert win.measured == [{"g": {"backend": "wine"}}], "the old pin is offered for deletion"


def test_gui_no_offer_when_nothing_was_downloaded(monkeypatch):
    _sync_threads(monkeypatch)
    monkeypatch.setattr(runners, "prefetch", lambda p, **k: [])
    win = _Win(_Cfg({"check_updates": True}, {"g": {"backend": "wine"}}))
    win._start_runner_prefetch()
    assert win.measured == []


@pytest.mark.parametrize(
    "cfg",
    [
        _Cfg({"check_updates": False}, {"g": {"backend": "wine"}}),
        _Cfg({"check_updates": True}, {"g": {"backend": "wine"}}, notice="recovered"),
    ],
)
def test_gui_no_prefetch_when_updates_are_off_or_config_untrusted(monkeypatch, cfg):
    _sync_threads(monkeypatch)
    monkeypatch.setattr(runners, "prefetch", lambda p, **k: pytest.fail("prefetched"))
    _Win(cfg)._start_runner_prefetch()


def test_gui_prefetch_failure_never_escapes(monkeypatch):
    _sync_threads(monkeypatch)

    def boom(p, **k):
        raise OSError("disk full")

    monkeypatch.setattr(runners, "prefetch", boom)
    win = _Win(_Cfg({"check_updates": True}, {"g": {"backend": "wine"}}))
    win._start_runner_prefetch()
    assert win.measured == []


def test_main_window_schedules_the_prefetch():
    src = Path(__file__).resolve().parents[1] / "src/nadare/gui/main_window.py"
    assert "_start_runner_prefetch" in src.read_text()


def test_a_window_s_own_timer_starts_no_runner_prefetch_in_unit_tests():
    """CI 1f9ef3c1: a leftover window's 20 s timer ran the real prefetch in a later test.

    Its thread installed runners and signalled a destroyed window, which crashed the worker.
    Tests of the prefetch call ``_real_start_runner_prefetch`` themselves.
    """
    pytest.importorskip("PyQt6")
    from nadare.gui.main_window import MainWindow
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    assert MainWindow._start_runner_prefetch(object()) is None  # touches nothing
    assert MainWindow._real_start_runner_prefetch is CleanupMixin._start_runner_prefetch
