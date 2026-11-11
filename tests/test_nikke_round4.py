# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - pytest fixtures imported from the sibling NIKKE test modules
"""NIKKE fixes from the b23 round-4 review (Flash, each claim checked against the code first).

- A launcherless Play updated the client under a running game: only Update checked the
  processes; Play checked the Flatpak run lock alone (three reviewers found it).
- The Play update ran without the operation lock, so another window's Remove could run then.
- A client download cut during the Store install left no "interrupted" marker.
- The game search followed links the game left in its prefix.
- Remove promises to forget the saved login; NIKKE's copy of the sign-in stayed.
- The launcherless game's output skipped the umu Stop-noise folding (#675).
"""

from __future__ import annotations

import contextlib
import json

import pytest
from _backend_helpers import make_context
from test_nikke_direct import rig  # noqa: F401 - fixture
from test_nikke_launcherless_hosts import make_prefix
from test_nikke_launcherless_launch import _NoProbe, make_install
from test_nikke_updates import _fake_launcherless, _nikke, api, env, installs  # noqa: F401

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core import paths, process, removal
from nadare.core.backends import BackendError
from nadare.core.config import Config


# ---- Play never updates under a running game ---------------------------------------------------
def test_play_does_not_update_while_the_game_runs_here(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [4242])
    backend = _nikke(env)
    notes = []
    backend.info = notes.append
    with contextlib.suppress(Exception):
        backend.launch()
    assert installs == []
    assert any("running" in n for n in notes)


def test_an_interrupted_update_waits_for_a_running_game(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    backend = _nikke(env)
    backend._update_marker().write_text("")
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [4242])
    with contextlib.suppress(Exception):
        backend.launch()
    assert installs == []


def test_play_does_not_update_while_another_window_holds_the_game(env, api, installs, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    backend = _nikke(env)
    other = process.OperationLock(backend.ctx.name).acquire()  # another window's Remove
    try:
        # Round 5: neither update nor start the game while another window changes its files.
        with pytest.raises(BackendError, match="in progress"):
            backend.launch()
    finally:
        other.release()
    assert installs == []
    assert ran == []


def test_the_play_update_holds_the_operation_lock(env, api, monkeypatch):
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    held = []

    def install_game(dest, *, emit=None, cancel=None, post=None, fetch=None):
        held.append(process.operation_in_progress(backend.ctx.name))
        (dest / "NIKKE" / "game" / "nikke.exe").write_bytes(b"a newer nikke.exe")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", install_game)
    backend = _nikke(env)
    assert backend.launch() == 0
    assert held == [True]
    assert not process.operation_in_progress(backend.ctx.name)  # released for the game
    assert ran == ["game"]


# ---- a client download cut during the Store install --------------------------------------------
def _direct_backend(tmp_path):
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine"}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def test_a_cut_client_download_during_install_leaves_the_interrupted_marker(
    tmp_path, rig, monkeypatch  # noqa: F811
):
    backend = _direct_backend(tmp_path)
    seen = []

    def cut(dest, *, emit=None, cancel=None, **kw):
        seen.append(backend._update_marker().exists())  # down before the first byte
        exe = dest / "NIKKE" / "game" / "nikke.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ already the new one")
        raise launcher_module.railrepo.RailError("connection reset")

    monkeypatch.setattr(launcher_module.nikke_source, "install_game", cut)
    backend.install()
    assert seen == [True]
    assert backend._update_marker().exists()  # a launcherless Play finishes the client first


def test_a_complete_install_leaves_no_marker(tmp_path, rig):  # noqa: F811
    backend = _direct_backend(tmp_path)
    backend.install()
    assert not backend._update_marker().exists()


# ---- the game search does not follow links ---------------------------------------------------
def test_find_game_follows_a_game_folder_the_user_moved_and_linked(tmp_path):
    """Round 5: skipping links hid a game folder moved to another disk and linked back."""
    prefix, launcher = make_prefix(tmp_path)
    game = prefix / "drive_c/NIKKE/NIKKE"
    moved = tmp_path / "other-disk" / "NIKKE"
    moved.parent.mkdir()
    game.rename(moved)
    game.symlink_to(moved, target_is_directory=True)
    files = h.find_game(launcher, prefix)
    assert files is not None and files.game_exe.name == "nikke.exe"


def test_find_game_still_finds_a_game_on_an_added_drive(tmp_path):
    """dosdevices letters are links by design: the drive root itself is followed."""
    prefix, launcher = make_prefix(tmp_path)
    (prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe").unlink()
    drive = tmp_path / "games-drive"
    (drive / "NIKKE" / "NIKKE" / "game").mkdir(parents=True)
    (drive / "NIKKE" / "NIKKE" / "game" / "nikke.exe").write_bytes(b"MZ")
    (prefix / "dosdevices").mkdir(exist_ok=True)
    (prefix / "dosdevices" / "s:").symlink_to(drive, target_is_directory=True)
    files = h.find_game(launcher, prefix)
    assert files is not None and files.game_exe.is_relative_to(drive.resolve())


# ---- Remove forgets the sign-in copy ---------------------------------------------------------
def test_remove_forgets_nadares_copy_of_the_nikke_sign_in(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("nikke", backend="launcher-nikke", prefix=str(tmp_path / "prefix"))
    session = paths.profiles_dir() / "nikke" / "nikke-session"
    session.mkdir(parents=True)
    (session / "intl_auth.txt").write_text(json.dumps({"token": "x"}))
    result = removal.remove_game(cfg, "nikke", applications_dir=tmp_path / "apps", save=False)
    assert not session.exists()
    assert result.login_forgotten
    assert (paths.profiles_dir() / "nikke").exists()  # the rest of the data folder stays


def test_remove_forgets_the_sign_in_of_a_moved_profile_folder(tmp_path, monkeypatch):
    """Round 5: a symlinked profile folder is allowed (BackendContext.data_dir)."""
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("nikke", backend="launcher-nikke", prefix=str(tmp_path / "prefix"))
    elsewhere = tmp_path / "disk" / "nikke"
    (elsewhere / "nikke-session").mkdir(parents=True)
    (elsewhere / "nikke-session" / "intl_auth.txt").write_text("{}")
    paths.profiles_dir().mkdir(parents=True, exist_ok=True)
    (paths.profiles_dir() / "nikke").symlink_to(elsewhere, target_is_directory=True)
    result = removal.remove_game(cfg, "nikke", applications_dir=tmp_path / "apps", save=False)
    assert not (elsewhere / "nikke-session").exists()
    assert result.login_forgotten


# ---- the launcherless game's output is folded like every other game's -------------------------
def test_the_launcherless_game_output_goes_through_the_stop_noise_filter(tmp_path, monkeypatch):
    from test_nikke_launcherless_launch import AUTH

    backend = make_install(tmp_path)
    backend._launcherless_plan()
    handlers = []

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        if h.INTL_HOST_NAME in argv[0]:
            out = argv[argv.index("--out") + 1]
            relative = out.split(":\\", 1)[1].replace("\\", "/")
            session_file = backend.prefix() / "drive_c" / relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        else:
            handlers.append(on_line)
            on_line("game says hello")  # the filter must exist while the game runs
        return 0

    monkeypatch.setattr(launcher_module, "stream", fake_stream)
    monkeypatch.setattr(
        NikkeBackend, "_prefix_command", lambda self, args, extra_env=None, **kw: (args, {})
    )
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(NikkeBackend, "_renderer_probe", lambda self: _NoProbe())
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )
    monkeypatch.setattr(NikkeBackend, "_update_before_play", lambda self: None)
    said = []
    backend.info = said.append
    assert backend.launch() == 0
    assert handlers == [backend._on_game_line]
    assert any("game says hello" in m for m in said)



def test_a_fifo_update_marker_does_not_hang_play_or_update(env, api, installs, monkeypatch):
    """Sweep round 4: the marker lives in the game folder; a FIFO there blocked os.open forever."""
    import os
    import threading

    backend = _nikke(env)
    os.mkfifo(backend._update_marker())
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])
    outcome: list[object] = []

    def run():
        try:
            backend.update()
            outcome.append("ok")
        except Exception as exc:  # noqa: BLE001 - the outcome is what is checked
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)
    if worker.is_alive():
        with open(backend._update_marker(), "rb"):  # let the blocked writer go
            pass
        worker.join(5)
        pytest.fail("the update blocked on a FIFO marker")
    assert installs == []
    assert isinstance(outcome[0], BackendError)


def test_play_works_where_the_state_folder_cannot_lock(env, api, installs, monkeypatch):
    """Round 6: flock failing with ENOLCK/ENOTSUP is not "another window is busy"."""
    ran = []
    _fake_launcherless(monkeypatch, ran)
    api["exe"] = b"a newer nikke.exe"
    monkeypatch.setattr(launcher_module, "scan_pids", lambda *a, **k: [])

    def no_lock(self):
        raise process.ProfileLockError("flock: No locks available")

    monkeypatch.setattr(process.OperationLock, "acquire", no_lock)
    monkeypatch.setattr(process, "operation_in_progress", lambda name: False)
    backend = _nikke(env)
    notes = []
    backend.info = notes.append
    assert backend.launch() == 0
    assert ran == ["game"]


def test_find_game_does_not_take_a_linked_exe(tmp_path):
    """Round 6: a linked folder is a moved game; a linked nikke.exe is not something we run."""
    prefix, launcher = make_prefix(tmp_path)
    exe = prefix / "drive_c/NIKKE/NIKKE/game/nikke.exe"
    exe.unlink()
    other = tmp_path / "elsewhere.exe"
    other.write_bytes(b"MZ")
    exe.symlink_to(other)
    assert h.find_game(launcher, prefix) is None
