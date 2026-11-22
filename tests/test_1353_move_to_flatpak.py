# SPDX-License-Identifier: GPL-3.0-or-later
"""#1353: the AppImage moves its data and settings into the Flatpak's folder, once.

The Flatpak has no home access (#1244), so a player who switches starts empty. The AppImage
renames ``~/.local/share/nadare`` and ``~/.config/nadare`` into ``~/.var/app/<id>/`` (no copy,
no Flatpak permission), rewrites the stored paths that pointed into them, and leaves a note
where the data went. It refuses while a game or the Flatpak runs, when the Flatpak already
has data (no merge), across filesystems, and inside the Flatpak itself. A crash part-way is
finished (or forgotten) at the next start from a journal.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nadare.core import flatpakmove, paths, runlock
from nadare.core.config import Config

APP = "io.github.frostlinuxdev.Nadare"


class FakeFlatpak:
    """``flatpak info`` / ``flatpak ps`` answers without a real Flatpak."""

    def __init__(self, installed=True, running=False):
        self.installed, self.running = installed, running
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        assert "env" in kwargs  # host helpers get a filtered environment
        self.calls.append(list(argv))
        if argv[1:2] == ["info"]:
            return subprocess.CompletedProcess(argv, 0 if self.installed else 1, "", "")
        if argv[1:2] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, APP + "\n" if self.running else "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for key, rel in (("DATA", ".local/share"), ("CONFIG", ".config"),
                     ("STATE", ".local/state"), ("CACHE", ".cache")):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(home / rel))
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    return home


def _appimage_world(home: Path) -> Config:
    """A b24 player: CZN inside Nadare's data folder, WuWa in ~/Games, one on another drive."""
    data = paths.data_dir()
    czn = data / "profiles" / "czn" / "prefix"
    (czn / "drive_c" / "CZN").mkdir(parents=True)
    (czn / "drive_c" / "CZN" / "game.exe").write_text("exe")
    runner = data / "runners" / "dwproton"
    (runner / "files" / "bin").mkdir(parents=True)
    wuwa = home / "Games" / "Nadare" / "WuWa"
    (wuwa / "install").mkdir(parents=True)
    (wuwa / "prefix" / "drive_c").mkdir(parents=True)
    cfg = Config.load(paths.config_file())
    cfg.add_profile("czn", backend="czn", title="CZN", prefix=str(czn),
                    runner=str(runner))
    cfg.add_profile("wuwa", backend="direct-wuwa", title="WuWa",
                    install_dir=str(wuwa / "install"), prefix=str(wuwa / "prefix"),
                    owned_dirs=[str(wuwa)])
    cfg.add_profile("far", backend="direct-pgr", title="PGR",
                    install_dir="/srv/games/pgr", prefix="/srv/games/pgr-prefix")
    cfg.save()
    return cfg


def _flatpak_dirs(home: Path) -> tuple[Path, Path]:
    base = home / ".var" / "app" / APP
    return base / "data" / "nadare", base / "config" / "nadare"


def test_targets_are_where_the_flatpak_reads(home):
    assert flatpakmove.targets(home) == _flatpak_dirs(home)


def test_the_move_renames_and_rewrites_paths(home):
    cfg = _appimage_world(home)
    old_data, old_config = paths.data_dir(), paths.config_dir()
    exe_inode = os.stat(old_data / "profiles/czn/prefix/drive_c/CZN/game.exe").st_ino
    notes = flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    new_data, new_config = _flatpak_dirs(home)
    # A rename: the very same file, not a copy.
    moved_exe = new_data / "profiles/czn/prefix/drive_c/CZN/game.exe"
    assert os.stat(moved_exe).st_ino == exe_inode
    assert not old_data.exists()
    saved = json.loads((new_config / "config.json").read_text())
    czn, wuwa, far = (saved["profiles"][n] for n in ("czn", "wuwa", "far"))
    assert czn["prefix"] == str(new_data / "profiles/czn/prefix")
    assert czn["runner"] == str(new_data / "runners/dwproton")
    assert wuwa["prefix"] == str(home / "Games/Nadare/WuWa/prefix")  # outside: untouched
    assert far["install_dir"] == "/srv/games/pgr"
    # The AppImage starts empty and is told where its data went; nothing is deleted.
    assert not (old_config / "config.json").exists()
    assert (old_config / "config.json.moved-to-flatpak").exists()
    assert flatpakmove.moved_note(home=home)
    assert not (old_config / flatpakmove.JOURNAL).exists()
    # A game the Flatpak cannot see is named.
    assert any("PGR" in n and "/srv/games/pgr" in n for n in notes)
    assert not any("WuWa" in n for n in notes)


def test_stored_paths_with_a_tilde_are_rewritten(home):
    cfg = _appimage_world(home)
    cfg.profiles["czn"]["prefix"] = "~/.local/share/nadare/profiles/czn/prefix"
    cfg.save()
    flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    new_data, new_config = _flatpak_dirs(home)
    saved = json.loads((new_config / "config.json").read_text())
    assert saved["profiles"]["czn"]["prefix"] == str(new_data / "profiles/czn/prefix")


@pytest.mark.parametrize(
    ("fake", "why"),
    [
        (FakeFlatpak(installed=False), "not installed"),
        (FakeFlatpak(running=True), "running"),
    ],
)
def test_refuses_without_an_idle_installed_flatpak(home, fake, why):
    cfg = _appimage_world(home)
    reason = flatpakmove.refusal(cfg, home=home, run=fake)
    assert reason and why in reason
    with pytest.raises(flatpakmove.MoveRefused):
        flatpakmove.move(cfg, home=home, run=fake)
    assert paths.data_dir().is_dir() and paths.config_file().is_file()


def test_refuses_when_the_flatpak_already_has_games(home):
    cfg = _appimage_world(home)
    new_data, new_config = _flatpak_dirs(home)
    (new_data / "profiles" / "genshin").mkdir(parents=True)
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "already" in reason


def test_refuses_when_the_flatpak_config_has_games(home):
    cfg = _appimage_world(home)
    _, new_config = _flatpak_dirs(home)
    new_config.mkdir(parents=True)
    (new_config / "config.json").write_text(json.dumps({"profiles": {"hsr": {}}}))
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "already" in reason


def test_a_flatpak_start_without_games_is_set_aside_not_deleted(home):
    """The Flatpak was opened once (settings, a runner): kept beside, the move goes ahead."""
    cfg = _appimage_world(home)
    new_data, new_config = _flatpak_dirs(home)
    (new_data / "runners" / "ge").mkdir(parents=True)
    new_config.mkdir(parents=True)
    (new_config / "config.json").write_text(json.dumps({"profiles": {}, "theme": "x"}))
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak()) is None
    flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    asides = [p for p in new_data.parent.iterdir() if p.name.startswith("nadare.before-move")]
    assert asides and (asides[0] / "runners" / "ge").is_dir()
    conf_asides = [p for p in new_config.parent.iterdir()
                   if p.name.startswith("nadare.before-move")]
    assert conf_asides and (conf_asides[0] / "config.json").is_file()
    assert (new_data / "profiles" / "czn").is_dir()


def test_the_running_appimage_saves_into_the_flatpak_afterwards(home):
    """A save after the move must not recreate the old config with moved-away paths."""
    cfg = _appimage_world(home)
    flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    cfg.save()
    assert not paths.config_file().exists()
    new_data, new_config = _flatpak_dirs(home)
    saved = json.loads((new_config / "config.json").read_text())
    assert saved["profiles"]["czn"]["prefix"] == str(new_data / "profiles/czn/prefix")


def test_an_empty_flatpak_folder_is_not_data(home):
    """The Flatpak creates empty folders on its first start: that is not a merge."""
    cfg = _appimage_world(home)
    new_data, new_config = _flatpak_dirs(home)
    new_data.mkdir(parents=True)
    new_config.mkdir(parents=True)
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak()) is None


def test_refuses_while_a_game_runs(home):
    cfg = _appimage_world(home)
    with runlock.hold(cfg.profiles["wuwa"]["prefix"]):
        reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "WuWa" in reason


def test_refuses_across_filesystems(home, monkeypatch):
    cfg = _appimage_world(home)
    monkeypatch.setattr(flatpakmove, "_same_filesystem", lambda a, b: False)
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "drive" in reason


def test_refuses_inside_the_flatpak(home, monkeypatch):
    cfg = _appimage_world(home)
    monkeypatch.setattr(flatpakmove.sandbox, "in_flatpak", lambda: True)
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())


def test_refuses_with_an_unfinished_import_move(home):
    cfg = _appimage_world(home)
    journal = paths.config_dir() / ".config.json.importmove.json"
    journal.write_text("{}")
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "unfinished" in reason


def _crash_after(cfg, home, step, monkeypatch):
    """Run the move but die right after ``step``."""

    class Died(BaseException):
        pass

    real = flatpakmove._step

    def stepper(name, action):
        real(name, action)
        if name == step:
            raise Died

    monkeypatch.setattr(flatpakmove, "_step", stepper)
    with pytest.raises(Died):
        flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    monkeypatch.setattr(flatpakmove, "_step", real)


def test_a_crash_before_the_rename_is_forgotten(home, monkeypatch):
    cfg = _appimage_world(home)
    _crash_after(cfg, home, "journal", monkeypatch)
    notes = flatpakmove.finish_interrupted(home=home)
    assert paths.data_dir().is_dir() and paths.config_file().is_file()
    assert not (paths.config_dir() / flatpakmove.JOURNAL).exists()
    assert notes


def test_a_crash_after_the_rename_is_finished(home, monkeypatch):
    cfg = _appimage_world(home)
    _crash_after(cfg, home, "data", monkeypatch)
    new_data, new_config = _flatpak_dirs(home)
    assert new_data.is_dir() and not paths.data_dir().exists()
    notes = flatpakmove.finish_interrupted(home=home)
    saved = json.loads((new_config / "config.json").read_text())
    assert saved["profiles"]["czn"]["prefix"] == str(new_data / "profiles/czn/prefix")
    assert not paths.config_file().exists()
    assert not (paths.config_dir() / flatpakmove.JOURNAL).exists()
    assert notes and flatpakmove.moved_note(home=home)


def test_a_crash_after_the_config_is_written_is_finished(home, monkeypatch):
    cfg = _appimage_world(home)
    _crash_after(cfg, home, "config", monkeypatch)
    flatpakmove.finish_interrupted(home=home)
    _, new_config = _flatpak_dirs(home)
    assert (new_config / "config.json").is_file()
    assert not paths.config_file().exists()
    assert not (paths.config_dir() / flatpakmove.JOURNAL).exists()


def test_both_halves_present_is_left_alone(home, monkeypatch):
    cfg = _appimage_world(home)
    _crash_after(cfg, home, "data", monkeypatch)
    paths.data_dir().mkdir(parents=True)  # something made a new data folder since
    (paths.data_dir() / "x").write_text("x")
    notes = flatpakmove.finish_interrupted(home=home)
    assert (paths.config_dir() / flatpakmove.JOURNAL).exists()
    assert (paths.data_dir() / "x").exists() and _flatpak_dirs(home)[0].is_dir()
    assert notes and "kept" in notes[0]


def test_no_journal_means_nothing(home):
    _appimage_world(home)
    assert flatpakmove.finish_interrupted(home=home) == []
    assert flatpakmove.moved_note(home=home) is None


# --- #1479: an inspection that failed is not a safe state ------------------------------------


class _PsFails(FakeFlatpak):
    def __init__(self, how):
        super().__init__()
        self.how = how

    def __call__(self, argv, **kwargs):
        if argv[1:2] == ["ps"]:
            if self.how == "timeout":
                raise subprocess.TimeoutExpired(argv, 20)
            if self.how == "oserror":
                raise OSError("flatpak vanished")
            return subprocess.CompletedProcess(argv, 1, "", "error: cannot list instances")
        return super().__call__(argv, **kwargs)


def _untouched(home: Path) -> None:
    new_data, new_config = _flatpak_dirs(home)
    assert paths.data_dir().is_dir() and paths.config_file().is_file()
    assert not (paths.config_dir() / flatpakmove.JOURNAL).exists()


@pytest.mark.parametrize("how", ["timeout", "oserror", "nonzero"])
def test_an_unknown_flatpak_running_state_refuses(home, how):
    cfg = _appimage_world(home)
    reason = flatpakmove.refusal(cfg, home=home, run=_PsFails(how))
    assert reason and "could not check" in reason.lower()
    with pytest.raises(flatpakmove.MoveRefused):
        flatpakmove.move(cfg, home=home, run=_PsFails(how))
    _untouched(home)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '"profiles"',
        "\xff\xfe",
        '{"profiles": ["hsr"]}',
        '{"profiles": 5}',
        '{"profiles": []}',
    ],
)
def test_a_flatpak_config_that_cannot_be_read_refuses(home, text):
    cfg = _appimage_world(home)
    _, new_config = _flatpak_dirs(home)
    new_config.mkdir(parents=True)
    (new_config / "config.json").write_bytes(text.encode("latin-1"))
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "could not read" in reason.lower()
    with pytest.raises(flatpakmove.MoveRefused):
        flatpakmove.move(cfg, home=home, run=FakeFlatpak())
    _untouched(home)
    assert (new_config / "config.json").read_bytes() == text.encode("latin-1")


def test_an_unreadable_flatpak_profiles_folder_refuses(home, monkeypatch):
    cfg = _appimage_world(home)
    new_data, _ = _flatpak_dirs(home)
    (new_data / "profiles").mkdir(parents=True)
    real = Path.iterdir

    def denied(self):
        if self == new_data / "profiles":
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", denied)
    reason = flatpakmove.refusal(cfg, home=home, run=FakeFlatpak())
    assert reason and "could not read" in reason.lower()
    _untouched(home)


def test_a_flatpak_config_without_profiles_still_moves(home):
    """Proven empty setups keep moving (the rename-aside path)."""
    cfg = _appimage_world(home)
    _, new_config = _flatpak_dirs(home)
    new_config.mkdir(parents=True)
    (new_config / "config.json").write_text(json.dumps({"profiles": {}}))
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak()) is None


def test_no_flatpak_folders_at_all_still_moves(home):
    cfg = _appimage_world(home)
    assert flatpakmove.refusal(cfg, home=home, run=FakeFlatpak()) is None
