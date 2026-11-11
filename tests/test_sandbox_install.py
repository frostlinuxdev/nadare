# SPDX-License-Identifier: GPL-3.0-or-later
"""A publisher's installer runs in the Sandbox too, not only Play.

The NIKKE launcher compromise came through code the publisher's installer and launcher run, so the
install step is where hiding the home folder matters most. The same rules as Play apply: on by
default, an explicit off keeps it off, an explicit on with no bubblewrap stops the install.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import launcher
from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import gamesandbox, linkrelay, sevenzip
from nadare.core.backends import BackendError

INNER = "nikkeminiloader.exe"


@pytest.fixture
def default_on(monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


@pytest.fixture
def no_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox, "locate", lambda **_: gamesandbox.Located(binary=None))


def _install_launcher(prefix: Path) -> None:
    write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    installer = write_exe(tmp_path / "downloads" / "nikke-wrapper.exe", "MZ")
    calls: list[list[str]] = []

    def fake_stream(argv, env, on_line=None, cancel=None):
        calls.append(list(argv))
        _install_launcher(tmp_path / "prefix")
        return 0

    def fake_extract(sevenz, archive, outdir, run=None):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / INNER).write_text("MZ")

    monkeypatch.setattr(ak, "stream", fake_stream)
    monkeypatch.setattr(wg, "stream", fake_stream)
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
    monkeypatch.setattr(launcher, "_extract_nsis", fake_extract)
    monkeypatch.setattr(sevenzip, "find", lambda **kw: "/usr/bin/7z")
    monkeypatch.delenv("NADARE_NIKKE_SILENT", raising=False)
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
    return calls, installer


def _backend(tmp_path: Path, events: list, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    ctx = make_context(tmp_path, "launcher-nikke", values)
    ctx.emit = events.append
    return NikkeBackend(ctx)


def _reports(events: list) -> list[tuple[bool, str]]:
    return [(e.data["sandbox"], e.message) for e in events if "sandbox" in (e.data or {})]


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, flag in enumerate(argv) if flag == "--ro-bind"]


# ---- the wrapper ---------------------------------------------------------------------------
def test_wrap_binds_extra_files_read_only_and_skips_missing_ones(tmp_path):
    present = write_exe(tmp_path / "setup.exe", "MZ")
    argv, _env = gamesandbox.wrap(
        ["wine", str(present)],
        {},
        prefix=tmp_path / "prefix",
        home=tmp_path / "home",
        read_only=[present, tmp_path / "missing.exe"],
    )
    binds = _ro_binds(argv)
    assert str(present.resolve()) in binds
    assert str((tmp_path / "missing.exe").resolve()) not in binds
    assert argv[argv.index("--") + 1 :] == ["wine", str(present)]


# ---- NIKKE's silent downloader ----------------------------------------------------------------
def test_the_silent_downloader_runs_in_the_sandbox_by_default(tmp_path, rig, default_on, has_bwrap):
    calls, _installer = rig
    events: list = []
    _backend(tmp_path, events).install()
    assert len(calls) == 1 and calls[0][0] == "bwrap"
    inner = calls[0][calls[0].index("--") + 1 :]
    assert inner[-3:] == ["/Silent", "/Dst=C:\\NIKKE", "/StartAfterInstall=false"]
    assert [ok for ok, _msg in _reports(events)] == [True]
    assert "installer" in _reports(events)[0][1]


def test_the_installer_window_runs_in_the_sandbox_with_the_installer_readable(
    tmp_path, rig, default_on, has_bwrap, monkeypatch
):
    calls, installer = rig
    monkeypatch.setattr(sevenzip, "find", lambda **kw: None)  # no silent path: the window
    _backend(tmp_path, []).install()
    assert len(calls) == 1 and calls[0][0] == "bwrap"
    assert str(installer.resolve()) in _ro_binds(calls[0])
    assert str(installer) in calls[0][calls[0].index("--") + 1 :]


def test_an_explicit_off_installs_without_it(tmp_path, rig, default_on, has_bwrap):
    calls, _installer = rig
    _backend(tmp_path, [], secure_mode=False).install()
    assert calls[0][0] != "bwrap"


def test_default_on_without_bubblewrap_installs_unprotected_and_says_so(
    tmp_path, rig, default_on, no_bwrap
):
    calls, _installer = rig
    events: list = []
    _backend(tmp_path, events).install()
    assert calls[0][0] != "bwrap"
    reports = _reports(events)
    assert reports and reports[0][0] is False
    assert "installer runs without it" in reports[0][1]


def test_an_explicit_on_without_bubblewrap_stops_the_install(tmp_path, rig, default_on, no_bwrap):
    calls, _installer = rig
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, [], secure_mode=True).install()
    assert "bubblewrap" in str(caught.value)
    assert calls == []


def test_an_install_opens_no_link_relay(tmp_path, rig, default_on, has_bwrap, monkeypatch):
    started: list = []

    class Relay:
        def __init__(self, *a, **k):
            started.append(True)

    monkeypatch.setattr(linkrelay, "LinkRelay", Relay)
    _backend(tmp_path, []).install()
    assert started == []


def test_play_still_says_game_not_installer(tmp_path, default_on, has_bwrap):
    events: list = []
    _install_launcher(tmp_path / "prefix")
    _backend(tmp_path, events).build_launch()
    reports = _reports(events)
    assert reports and reports[0][0] is True and "game" in reports[0][1]
    assert "installer" not in reports[0][1]
