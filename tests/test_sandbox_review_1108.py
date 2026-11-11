# SPDX-License-Identifier: GPL-3.0-or-later
"""Findings of the 2026-11-08 Sandbox review, each verified before the fix."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import gamesandbox


def _binds(argv, flag="--bind"):
    return [(argv[i + 1], argv[i + 2]) for i, f in enumerate(argv) if f == flag]


# ---- 1. a path reached through a symlink in the hidden home ------------------------------------
def test_a_symlinked_prefix_is_also_bound_where_wine_looks_for_it(tmp_path):
    home = tmp_path / "home"
    real = tmp_path / "otherdisk" / "Games" / "p"
    (real / "drive_c").mkdir(parents=True)
    home.mkdir(exist_ok=True)
    (home / "Games").symlink_to(tmp_path / "otherdisk" / "Games", target_is_directory=True)
    prefix = home / "Games" / "p"
    argv, _env = gamesandbox.wrap(["wine", "x.exe"], {}, prefix=prefix, home=home)
    binds = _binds(argv)
    assert (str(real.resolve()), str(real.resolve())) in binds
    assert (str(real.resolve()), str(prefix)) in binds


def test_a_symlinked_program_in_the_home_is_bound_at_its_own_path(tmp_path):
    home = tmp_path / "home"
    real = tmp_path / "home" / ".local" / "share" / "pipx" / "umu-run"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    (home / ".local" / "bin").mkdir(parents=True)
    link = home / ".local" / "bin" / "umu-run"
    link.symlink_to(real)
    argv, _env = gamesandbox.wrap([str(link), "x.exe"], {}, prefix=tmp_path / "p", home=home)
    assert (str(real.resolve()), str(link)) in _binds(argv, "--ro-bind")


def test_a_plain_path_gets_one_bind_only(tmp_path):
    prefix = tmp_path / "p"
    prefix.mkdir()
    argv, _env = gamesandbox.wrap(["wine"], {}, prefix=prefix, home=tmp_path / "home")
    assert [b for b in _binds(argv) if b[0] == str(prefix.resolve())] == [
        (str(prefix.resolve()), str(prefix.resolve()))
    ]


# ---- 5. a hand-edited string under extra ------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False), ("yes", True), ("no", False),
     ("maybe", None)],
)
def test_a_string_under_extra_means_what_it_says(value, expected):
    assert gamesandbox.choice({"extra": {"secure_mode": value}}) is expected


# ---- 6. a personal folder is never bound into the Sandbox ---------------------------------------
@pytest.mark.parametrize("folder", ["Documents", ".ssh", ".config", "Downloads"])
def test_a_personal_folder_as_game_folder_is_refused(tmp_path, folder):
    home = tmp_path / "home"
    (home / folder).mkdir(parents=True)
    with pytest.raises(gamesandbox.BroadBindError):
        gamesandbox.wrap(["wine"], {}, prefix=tmp_path / "p", install_dir=home / folder, home=home)


def test_an_ordinary_game_folder_in_the_home_is_still_fine(tmp_path):
    home = tmp_path / "home"
    (home / "Games" / "Thing").mkdir(parents=True)
    folder = home / "Games" / "Thing"
    gamesandbox.wrap(["wine"], {}, prefix=tmp_path / "p", install_dir=folder, home=home)


# ---- 2 and 3. the pill never claims protection the launch cannot give ----------------------------
try:
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameView

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False


@pytest.fixture
def view():
    if not HAVE_QT:
        pytest.skip("PyQt6 is not importable")
    app = QApplication.instance() or QApplication([])
    page = GameView()
    page.set_profile("g", {"backend": "direct-pgr", "title": "G"})
    yield page
    page.deleteLater()
    app.processEvents()


@pytest.mark.parametrize(
    "kwargs", [{"ready": False}, {"blocked": "Not available in the Flatpak version yet."}]
)
def test_pressing_play_does_not_make_up_a_sandbox(view, kwargs):
    blocked = kwargs.pop("blocked", "")
    view.set_sandbox(True, True, blocked, **kwargs)
    assert view.sandbox_pill.isHidden()
    view.set_busy("launch")
    assert view.sandbox_pill.isHidden() or view.sandbox_pill.text() != "SANDBOX"


def test_ready_means_a_bubblewrap_that_really_starts(monkeypatch):
    gamesandbox._READY_CACHE.clear()
    monkeypatch.setattr(gamesandbox, "locate", lambda **_: gamesandbox.Located(
        binary=None, found=True, why="uid map"))
    assert gamesandbox.ready() is False
    gamesandbox._READY_CACHE.clear()
    bwrap = gamesandbox.Located(binary="/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "locate", lambda **_: bwrap)
    assert gamesandbox.ready() is True
    gamesandbox._READY_CACHE.clear()


def test_ready_is_cached_so_opening_a_page_does_not_start_bwrap_each_time(monkeypatch):
    gamesandbox._READY_CACHE.clear()
    calls = []
    monkeypatch.setattr(gamesandbox, "locate",
                        lambda **_: calls.append(1) or gamesandbox.Located(binary="/usr/bin/bwrap"))
    gamesandbox.ready()
    gamesandbox.ready()
    assert calls == [1]
    gamesandbox._READY_CACHE.clear()


# ---- 4. a sandboxed installer that left nothing says why it probably happened ------------------
def test_a_sandboxed_installer_that_left_no_launcher_explains_the_hidden_home(
    tmp_path, monkeypatch
):
    from _backend_helpers import fake_launcher_backend, make_context, write_exe

    from nadare.backends import winegame as wg
    from nadare.backends.winegame import WineGameBackend
    from nadare.core.backends import BackendError

    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    installer = write_exe(tmp_path / "dl" / "nte-setup.exe", "MZ")
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
    monkeypatch.setattr(wg, "stream", lambda argv, env, on_line=None, cancel=None: 0)
    (tmp_path / "prefix" / "drive_c").mkdir(parents=True)
    launcher_cls = fake_launcher_backend()
    backend = launcher_cls(make_context(tmp_path, launcher_cls.kind, {
        "prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine"}))
    with pytest.raises(BackendError) as caught:
        backend.install()
    message = str(caught.value)
    assert "Sandbox" in message and "C:" in message
