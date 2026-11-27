# SPDX-License-Identifier: GPL-3.0-or-later
"""The Strinova launcher updates the game itself; Nadare must follow it, never undo it.

On 2026-11-27 the publisher launcher patched 1.12.1.2 to 1.12.2.3 in place. Nadare's record
still said 1.12.1.2, and the next Play wrote that back into the launcher's ``config.ini``, so
the launcher would have patched already-patched files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import direct, winegame
from nadare.backends.wine import _query_matches
from nadare.core import directdl
from nadare.core import strinova_launcher as sl
from nadare.core.backends import BackendError

MZ = b"MZ" + b"\0" * 64
OLD = "1.12.1.2"
NEW = "1.12.2.3"
MID = "1.12.2.0"

#: What the launcher left after its own update (the AMD test desktop, 2026-11-27).
AFTER_LAUNCHER_UPDATE = (
    "[General]\r\n"
    f"CurrentGameVersion={NEW}\r\n"
    "TargetGameVersion=\r\n"
    "bPatchFinished=false\r\n"
    "IsAutoStart=false\r\n"
    "TransitionGameVersion=\r\n"
    "CurrentStage=INITSTAGE\r\n"
    "GamePath=C:/Strinova/Game\r\n"
    "PatchTime=0\r\n"
).encode()


@pytest.fixture(autouse=True)
def _no_webview2_download(monkeypatch):
    monkeypatch.setattr(direct.StrinovaBackend, "_ensure_webview2", lambda self: None)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    """The test host's own processes never count; a test that needs one plants it."""
    monkeypatch.setattr(winegame, "scan_pids", lambda query: [])


def _prefix(tmp_path):
    prefix = tmp_path / "pfx"
    game = prefix / "drive_c" / "Games" / "Strinova"
    game.mkdir(parents=True)
    (game / "Strinova.exe").write_bytes(MZ)
    launcher = sl.launcher_path(prefix)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(MZ)
    return prefix, game


def _ensure(prefix, game, version, *, linked=True):
    if linked and not os.path.lexists(sl.game_link(prefix)):
        sl._link_game(sl.game_link(prefix), game, prefix)  # an earlier Play made the link
    return sl.ensure_launcher(
        prefix,
        game,
        version=version,
        data_dir=prefix.parent / "data",
        download=lambda *a: pytest.fail("downloaded"),
        which=lambda c: pytest.fail("looked for 7-Zip"),
        run=lambda *a, **k: pytest.fail("ran 7-Zip"),
    )


def _config(**keys: str) -> bytes:
    return ("[General]\r\n" + "".join(f"{k}={v}\r\n" for k, v in keys.items())).encode()


def _keys(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            found[key] = value
    return found


# ---- config.ini: never write a version older than the launcher's own -------------------------


def test_the_launchers_own_update_is_left_byte_for_byte(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    _ensure(prefix, game, OLD)
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE


@pytest.mark.parametrize(
    "keys",
    [
        {"CurrentGameVersion": OLD, "TargetGameVersion": NEW, "CurrentStage": "PATCHSTAGE"},
        {"CurrentGameVersion": OLD, "TargetGameVersion": NEW, "CurrentStage": "VERIFYSTAGE"},
        {"CurrentGameVersion": OLD, "TransitionGameVersion": NEW, "CurrentStage": "INITSTAGE"},
        {"CurrentGameVersion": OLD, "TargetGameVersion": NEW},
    ],
    ids=["patching", "verifying", "transition", "target-ahead"],
)
def test_a_launcher_patch_in_progress_is_never_touched(tmp_path, keys):
    """Nadare's record (MID) is newer than the launcher's current version, but the launcher
    is already patching past it: only the patch guard keeps the file as it is."""
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    raw = _config(bPatchFinished="false", **keys)
    path.write_bytes(raw)
    _ensure(prefix, game, NEW)  # links Game first, so the next call is an ordinary Play
    path.write_bytes(raw)
    _ensure(prefix, game, MID)
    assert path.read_bytes() == raw


def test_the_same_version_is_not_rewritten(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    raw = _config(CurrentGameVersion=OLD, TargetGameVersion="", bPatchFinished="false")
    path.write_bytes(raw)
    _ensure(prefix, game, OLD)
    assert path.read_bytes() == raw


def test_versions_compare_as_numbers_not_text(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    raw = _config(CurrentGameVersion="1.12.10.0", CurrentStage="INITSTAGE")
    path.write_bytes(raw)
    _ensure(prefix, game, "1.12.9.0")
    assert path.read_bytes() == raw  # 1.12.10.0 is newer, though "1" < "9" as text

    path.write_bytes(_config(CurrentGameVersion="1.12.9.0", CurrentStage="INITSTAGE"))
    _ensure(prefix, game, "1.12.10.0")
    assert _keys(path)["CurrentGameVersion"] == "1.12.10.0"


def test_a_nadare_update_is_still_handed_to_the_launcher(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    path.write_bytes(_config(CurrentGameVersion=OLD, TargetGameVersion=OLD, IsAutoStart="false"))
    _ensure(prefix, game, NEW)
    keys = _keys(path)
    assert keys["CurrentGameVersion"] == NEW
    assert keys["TargetGameVersion"] == NEW
    assert keys["bPatchFinished"] == "true"
    assert keys["IsAutoStart"] == "false"


def test_a_nadare_update_over_a_stale_launcher_patch_resets_its_stage(tmp_path):
    """Nadare verified every file at its newer version; the launcher's old patch is moot."""
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    path.write_bytes(
        _config(
            CurrentGameVersion=OLD,
            TargetGameVersion=NEW,
            TransitionGameVersion=NEW,
            CurrentStage="PATCHSTAGE",
            bPatchFinished="false",
        )
    )
    _ensure(prefix, game, NEW)
    keys = _keys(path)
    assert keys["CurrentGameVersion"] == NEW
    assert keys["TargetGameVersion"] == NEW
    assert keys["TransitionGameVersion"] == ""
    assert keys["CurrentStage"] == "INITSTAGE"
    assert keys["bPatchFinished"] == "true"


@pytest.mark.parametrize("key", ["TargetGameVersion", "TransitionGameVersion"])
def test_a_launcher_patching_past_nadare_is_left_alone(tmp_path, key):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    raw = _config(CurrentGameVersion=OLD, CurrentStage="PATCHSTAGE", **{key: "1.12.3.0"})
    path.write_bytes(raw)
    _ensure(prefix, game, NEW)
    assert path.read_bytes() == raw


def test_a_malformed_launcher_version_is_replaced(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    path.write_bytes(_config(CurrentGameVersion="garbage", CurrentStage="INITSTAGE"))
    _ensure(prefix, game, OLD)
    assert _keys(path)["CurrentGameVersion"] == OLD


def test_the_last_duplicate_key_is_the_one_that_counts(tmp_path):
    prefix, game = _prefix(tmp_path)
    path = sl.config_path(prefix)
    raw = _config(CurrentGameVersion=OLD) + b"CurrentGameVersion=" + NEW.encode() + b"\r\n"
    path.write_bytes(raw)
    _ensure(prefix, game, MID)  # newer than the first value, older than the last
    assert path.read_bytes() == raw
    staged = _config(CurrentStage="INITSTAGE", CurrentGameVersion=OLD)
    staged += b"CurrentStage=PATCHSTAGE\r\n"
    path.write_bytes(staged)
    assert sl.newer_installed_version(prefix, game, "1.0") is None  # the last stage patches


@pytest.mark.parametrize(("a", "b"), [("1.12.2", "1.12.2.0"), ("1.12.2.0.0", "1.12.2")])
def test_trailing_zeros_do_not_make_a_version_newer(a, b):
    assert sl.version_key(a) == sl.version_key(b)
    assert sl.version_key("1.12.2.1") > sl.version_key("1.12.2")
    assert sl.version_key("0") == sl.version_key("0.0") == (0,)


def test_a_reused_prefix_is_told_this_installs_version(tmp_path):
    """The prefix's launcher last managed another, newer copy; Play repoints Game at this
    install, so the config must describe this one, not the other (Neon, 2026-11-27)."""
    prefix, game = _prefix(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    sl._link_game(sl.game_link(prefix), other, prefix)
    path = sl.config_path(prefix)
    path.write_bytes(
        _config(CurrentGameVersion=NEW, TargetGameVersion=NEW, CurrentStage="PATCHSTAGE")
    )
    _ensure(prefix, game, OLD, linked=False)
    keys = _keys(path)
    assert keys["CurrentGameVersion"] == OLD
    assert keys["TargetGameVersion"] == OLD
    assert keys["CurrentStage"] == "INITSTAGE"
    assert sl.newer_installed_version(prefix, game, OLD) is None


def test_a_config_that_cannot_be_written_leaves_the_link_where_it_was(tmp_path, monkeypatch):
    """Otherwise Game would point at this install under the other one's config, and the
    next adoption would record the other version here (Neon, 2026-11-27)."""
    prefix, game = _prefix(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    sl._link_game(sl.game_link(prefix), other, prefix)
    sl.config_path(prefix).write_bytes(AFTER_LAUNCHER_UPDATE)

    def refuse(*a, **k):
        raise sl.StrinovaLauncherError("disk full")

    monkeypatch.setattr(sl, "_merge_config", refuse)
    with pytest.raises(sl.StrinovaLauncherError):
        _ensure(prefix, game, OLD, linked=False)
    assert sl._resolved(sl.game_link(prefix)) == sl._resolved(other)
    assert sl.newer_installed_version(prefix, game, OLD) is None


def test_a_real_game_folder_in_the_prefix_keeps_its_config(tmp_path):
    """The official launcher's own install is not Nadare's: refuse before writing anything."""
    prefix, game = _prefix(tmp_path)
    sl.game_link(prefix).mkdir(parents=True)
    path = sl.config_path(prefix)
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    with pytest.raises(sl.StrinovaLauncherError, match="not a link"):
        _ensure(prefix, game, OLD, linked=False)
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE


def test_a_verified_tree_resets_a_stuck_launcher(tmp_path):
    """After Nadare's Repair verified every file at a version, a launcher patch that stopped
    part-way at that same version must not stay stuck (Neon, 2026-11-27)."""
    prefix, game = _prefix(tmp_path)
    sl._link_game(sl.game_link(prefix), game, prefix)
    path = sl.config_path(prefix)
    path.write_bytes(
        _config(CurrentGameVersion=NEW, TargetGameVersion="1.12.3.0", CurrentStage="VERIFYSTAGE")
    )
    assert sl.sync_verified_version(prefix, game, NEW) is True
    keys = _keys(path)
    assert (keys["CurrentGameVersion"], keys["TargetGameVersion"]) == (NEW, NEW)
    assert (keys["CurrentStage"], keys["TransitionGameVersion"]) == ("INITSTAGE", "")


def test_a_verified_tree_leaves_another_folders_config_alone(tmp_path):
    prefix, game = _prefix(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    sl._link_game(sl.game_link(prefix), other, prefix)
    path = sl.config_path(prefix)
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    assert sl.sync_verified_version(prefix, game, OLD) is False
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE


def test_a_verified_tree_creates_no_config(tmp_path):
    prefix, game = _prefix(tmp_path)
    sl._link_game(sl.game_link(prefix), game, prefix)
    assert sl.sync_verified_version(prefix, game, OLD) is False
    assert not sl.config_path(prefix).exists()


# ---- which version the launcher installed -----------------------------------------------------


def test_the_launchers_newer_version_is_reported(tmp_path):
    prefix, game = _prefix(tmp_path)
    _ensure(prefix, game, None)  # links Game -> the install
    sl.config_path(prefix).write_bytes(AFTER_LAUNCHER_UPDATE)
    assert sl.newer_installed_version(prefix, game, OLD) == NEW


@pytest.mark.parametrize("recorded", [None, NEW, "1.12.3.0", "not a version"])
def test_no_newer_version_without_an_older_record(tmp_path, recorded):
    prefix, game = _prefix(tmp_path)
    _ensure(prefix, game, None)
    sl.config_path(prefix).write_bytes(AFTER_LAUNCHER_UPDATE)
    assert sl.newer_installed_version(prefix, game, recorded) is None


@pytest.mark.parametrize(
    "raw",
    [
        _config(CurrentGameVersion=NEW, TargetGameVersion="1.12.3.0", CurrentStage="PATCHSTAGE"),
        _config(CurrentGameVersion=NEW, TransitionGameVersion="1.12.3.0"),
        _config(CurrentGameVersion="1.12.2.x", CurrentStage="INITSTAGE"),
        _config(CurrentGameVersion=" ", CurrentStage="INITSTAGE"),
        b"",
    ],
    ids=["patching", "transition", "malformed", "blank", "empty"],
)
def test_no_newer_version_while_patching_or_unreadable(tmp_path, raw):
    prefix, game = _prefix(tmp_path)
    _ensure(prefix, game, None)
    sl.config_path(prefix).write_bytes(raw)
    assert sl.newer_installed_version(prefix, game, OLD) is None


def test_no_newer_version_when_the_launcher_patched_another_folder(tmp_path):
    prefix, game = _prefix(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    sl.launcher_dir(prefix).mkdir(parents=True, exist_ok=True)
    os.symlink(other, sl.game_link(prefix), target_is_directory=True)
    sl.config_path(prefix).write_bytes(AFTER_LAUNCHER_UPDATE)
    assert sl.newer_installed_version(prefix, game, OLD) is None


def test_no_newer_version_without_a_game_link(tmp_path):
    prefix, game = _prefix(tmp_path)
    sl.config_path(prefix).write_bytes(AFTER_LAUNCHER_UPDATE)
    assert sl.newer_installed_version(prefix, game, OLD) is None


# ---- reading config.ini safely ----------------------------------------------------------------


def test_a_linked_config_is_refused_and_its_target_kept(tmp_path):
    prefix, game = _prefix(tmp_path)
    elsewhere = tmp_path / "victim.ini"
    elsewhere.write_bytes(b"keep me")
    os.symlink(elsewhere, sl.config_path(prefix))
    with pytest.raises(sl.StrinovaLauncherError, match="config.ini"):
        _ensure(prefix, game, OLD)
    assert elsewhere.read_bytes() == b"keep me"
    assert sl.newer_installed_version(prefix, game, "0.1") is None


def test_a_fifo_config_does_not_hang(tmp_path):
    prefix, game = _prefix(tmp_path)
    os.mkfifo(sl.config_path(prefix))
    with pytest.raises(sl.StrinovaLauncherError, match="config.ini"):
        _ensure(prefix, game, OLD)


def test_an_oversized_config_is_refused(tmp_path, monkeypatch):
    prefix, game = _prefix(tmp_path)
    monkeypatch.setattr(sl, "_CONFIG_MAX_BYTES", 64)
    sl.config_path(prefix).write_bytes(_config(Padding="x" * 200))
    with pytest.raises(sl.StrinovaLauncherError, match="config.ini"):
        _ensure(prefix, game, OLD)


# ---- the backend adopts the launcher's version ------------------------------------------------


def _backend(tmp_path, *, latest=None):
    ctx = make_context(tmp_path, "direct-strinova", {"prefix": str(tmp_path / "pfx")})
    backend = direct.StrinovaBackend(ctx)
    game = backend.install_dir() / "Strinova.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(MZ)
    backend._write_version_file(OLD, "strinova")
    if latest is not None:
        backend._write_latest(latest)
    (backend.prefix() / "drive_c").mkdir(parents=True, exist_ok=True)
    launcher = sl.launcher_path(backend.prefix())
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(MZ)
    _ensure(backend.prefix(), backend.install_dir(), None)  # the Game link
    return backend, game


def test_play_adopts_what_the_launcher_installed(tmp_path):
    backend, game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE
    assert backend._read_version_file()["version"] == NEW
    assert backend.status().data.get("update_available") is False


def test_adopting_moves_an_older_published_version_forward(tmp_path):
    backend, game = _backend(tmp_path, latest=OLD)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert backend._read_latest()["version"] == NEW
    assert backend.status().data.get("update_available") is False


def test_adopting_keeps_the_last_check_time(tmp_path):
    """Moving the cached version forward must not count as a fresh server check, or a newer
    release would stay hidden for hours (Neon, 2026-11-27)."""
    backend, game = _backend(tmp_path, latest=OLD)
    latest = backend._read_latest()
    latest["checked_at"] = 12345
    backend._latest_path().write_text(json.dumps(latest), encoding="utf-8")
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert backend._read_latest() == {"version": NEW, "checked_at": 12345}


def test_adopting_without_a_cached_check_writes_none(tmp_path):
    backend, game = _backend(tmp_path)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert backend._read_version_file()["version"] == NEW
    assert backend._read_latest() is None


def test_a_nadare_update_that_finished_meanwhile_is_not_undone(tmp_path, monkeypatch):
    """The record is read again under the lock: an update that wrote a newer one between the
    first read and the lock must win (Neon, 2026-11-27)."""
    backend, game = _backend(tmp_path, latest=NEW)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    real = directdl.install_lock

    def lock(root):
        backend._write_version_file("1.12.3.0", "strinova")
        return real(root)

    monkeypatch.setattr(directdl, "install_lock", lock)
    backend._direct_argv(game)
    assert backend._read_version_file()["version"] == "1.12.3.0"


def test_a_running_operation_adopts_nothing(tmp_path, monkeypatch):
    from nadare.core import process

    backend, game = _backend(tmp_path, latest=NEW)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    monkeypatch.setattr(process, "operation_in_progress", lambda name: True)
    assert backend._adopt_launcher_version() is None
    assert backend._read_version_file()["version"] == OLD


def test_nothing_is_adopted_while_another_flatpak_window_plays(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path, latest=NEW)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: True)
    assert backend._adopt_launcher_version() is None
    assert backend._read_version_file()["version"] == OLD


def test_play_adopts_nothing_while_the_launcher_runs(tmp_path, monkeypatch):
    backend, game = _backend(tmp_path, latest=NEW)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    monkeypatch.setattr(backend, "_launcher_running", lambda: True)
    assert backend._adopt_launcher_version() is None
    assert backend._read_version_file()["version"] == OLD


def test_a_launcher_started_while_waiting_for_the_lock_adopts_nothing(tmp_path, monkeypatch):
    """The busy check runs again under the install lock: a launcher that opened in between
    may be patching, so its config is not read as finished (Neon, 2026-11-27)."""
    backend, game = _backend(tmp_path, latest=NEW)
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    lock = directdl.install_lock
    started = {"now": False}

    def lock_then_start(path):
        started["now"] = True  # the launcher opens while Nadare takes the lock
        return lock(path)

    monkeypatch.setattr(directdl, "install_lock", lock_then_start)
    monkeypatch.setattr(backend, "_launcher_running", lambda: started["now"])
    assert backend._adopt_launcher_version() is None
    assert backend._read_version_file()["version"] == OLD


def test_adopting_never_moves_the_published_version_back(tmp_path):
    backend, game = _backend(tmp_path, latest="1.12.3.0")
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert backend._read_latest()["version"] == "1.12.3.0"
    assert backend.status().data.get("update_available") is True


def test_an_unfinished_nadare_install_adopts_nothing(tmp_path):
    backend, game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._write_install_marker()
    backend._direct_argv(game)
    assert backend._read_version_file()["version"] == OLD
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE  # still never downgraded


def test_a_busy_nadare_sync_adopts_nothing(tmp_path):
    backend, game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())
    path.write_bytes(AFTER_LAUNCHER_UPDATE)
    with directdl.install_lock(backend.install_dir()):
        backend._direct_argv(game)
    assert backend._read_version_file()["version"] == OLD
    assert path.read_bytes() == AFTER_LAUNCHER_UPDATE


def test_adoption_is_said_in_the_log(tmp_path):
    backend, game = _backend(tmp_path, latest=NEW)
    events: list = []
    backend.ctx.emit = events.append
    sl.config_path(backend.prefix()).write_bytes(AFTER_LAUNCHER_UPDATE)
    backend._direct_argv(game)
    assert any(
        event.kind == "log" and NEW in event.message and "launcher" in event.message
        for event in events
    )


def _fake_launch(backend, monkeypatch, *, during):
    def launch(self):
        during()
        return 0

    monkeypatch.setattr(direct.DirectBackend, "launch", launch)


def test_an_update_made_while_playing_is_adopted_when_the_launcher_closes(
    tmp_path, monkeypatch
):
    backend, _game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())
    path.write_bytes(_config(CurrentGameVersion=OLD, CurrentStage="INITSTAGE"))
    _fake_launch(backend, monkeypatch, during=lambda: path.write_bytes(AFTER_LAUNCHER_UPDATE))
    monkeypatch.setattr(backend, "_launcher_running", lambda: False)
    assert backend.launch() == 0
    assert backend._read_version_file()["version"] == NEW


def test_nothing_is_adopted_while_the_launcher_still_runs(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())
    _fake_launch(backend, monkeypatch, during=lambda: path.write_bytes(AFTER_LAUNCHER_UPDATE))
    monkeypatch.setattr(backend, "_launcher_running", lambda: True)
    backend.launch()
    assert backend._read_version_file()["version"] == OLD


def test_a_failed_launch_still_returns_its_error(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path, latest=NEW)

    def launch(self):
        raise BackendError("no runner", 3)

    monkeypatch.setattr(direct.DirectBackend, "launch", launch)
    monkeypatch.setattr(backend, "_launcher_running", lambda: False)
    with pytest.raises(BackendError, match="no runner"):
        backend.launch()


def test_an_unreadable_config_after_exit_does_not_fail_the_launch(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path, latest=NEW)
    path = sl.config_path(backend.prefix())

    def plant_link():
        os.symlink(tmp_path / "nowhere", path)

    _fake_launch(backend, monkeypatch, during=plant_link)
    monkeypatch.setattr(backend, "_launcher_running", lambda: False)
    assert backend.launch() == 0
    assert backend._read_version_file()["version"] == OLD


# ---- Nadare does not patch under the launcher -------------------------------------------------


LAUNCHER_PROCESSES = [
    "Strinova.exe",
    "hpatchz.exe",
    "hpatch.exe",
    "UpdateGUI.exe",
    "startup_launcher.exe",
]


def _plant(monkeypatch, name, prefix):
    env = {"WINEPREFIX": str(prefix)}

    def scan(query):
        return [1] if _query_matches(query, [name], env) else []

    monkeypatch.setattr(winegame, "scan_pids", scan)


@pytest.mark.parametrize("name", LAUNCHER_PROCESSES)
@pytest.mark.parametrize("operation", ["update", "repair"])
def test_update_and_repair_wait_for_the_launcher(tmp_path, monkeypatch, name, operation):
    backend, _game = _backend(tmp_path)
    _plant(monkeypatch, name, backend.prefix())
    synced: list = []
    monkeypatch.setattr(backend, "_before_sync", lambda *a, **k: synced.append(a))
    with pytest.raises(BackendError, match="launcher") as raised:
        getattr(backend, operation)()
    assert raised.value.code == 8
    assert synced == []
    assert not backend.status().running  # Playing still means the game, not the launcher


def test_a_launcher_in_another_prefix_does_not_count(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path)
    _plant(monkeypatch, "hpatchz.exe", tmp_path / "another-prefix")
    backend._refuse_if_running("updating it")


def test_a_launch_from_another_flatpak_window_counts(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: True)
    with pytest.raises(BackendError) as raised:
        backend._refuse_if_running("updating it")
    assert raised.value.code == 8


def test_update_runs_when_nothing_of_strinova_runs(tmp_path, monkeypatch):
    backend, _game = _backend(tmp_path)
    monkeypatch.setattr(backend, "_running_elsewhere", lambda: False)
    backend._refuse_if_running("updating it")

