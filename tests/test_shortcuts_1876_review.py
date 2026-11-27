# SPDX-License-Identifier: GPL-3.0-or-later
"""#1876 review: the cases the first round of shortcut tests did not reach.

- In the Flatpak, a host program the sandbox cannot see is asked about on the host; when the
  host cannot answer, the entry is not stale (a working AppImage in ~/Downloads stays).
- Steam start-up repair never points a broken shortcut at a broken running Nadare.
- A launch refused for an unaccepted account notice or an unreadable config also notifies.
- A symlinked entry is never read; Desktop entries are repaired with their own mode.
- ``Exec`` is read from ``[Desktop Entry]`` only, with spaces round ``=`` and a tab separator.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
from pathlib import Path

import pytest
from test_shortcuts_1876 import _config, _menu_entry, _NotInstalled
from test_steam_shortcut_ownership import _entry, _read, _write

from nadare import app, launchnotice
from nadare.core import sandbox, shortcuts
from nadare.core.config import Config


@pytest.fixture
def notes(monkeypatch):
    sent: list[tuple[str, str]] = []
    from nadare.gui import notify

    monkeypatch.setattr(notify, "send", lambda s, b="": sent.append((s, b)) or True)
    return sent


@pytest.fixture
def running(tmp_path) -> list[str]:
    """How this Nadare starts: a file that exists, so only the entry decides."""
    exe = tmp_path / "running" / "Nadare-new.AppImage"
    exe.parent.mkdir()
    exe.write_text("")
    return [str(exe)]


# ---- Flatpak: ask the host, never guess ------------------------------------------------
@pytest.fixture
def flatpak(monkeypatch):
    """Inside the Flatpak; records what is asked of the host and answers with ``rc``."""
    asked: list[list[str]] = []
    state = {"rc": 0, "raise": False}

    def run(argv, **kwargs):
        asked.append(list(argv))
        if state["raise"]:
            raise OSError("no flatpak-spawn")
        return subprocess.CompletedProcess(argv, state["rc"], "", "")

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(shortcuts.subprocess, "run", run)
    return asked, state


def test_in_the_flatpak_a_host_program_that_exists_is_not_stale(tmp_path, flatpak):
    asked, state = flatpak
    state["rc"] = 0  # the host has it
    hidden = "/home/someone/Downloads/Nadare-b27.AppImage"  # not shared with the sandbox
    assert shortcuts._program_missing(hidden) is False
    assert asked and asked[-1][-2:] == ["-e", hidden]
    assert asked[-1][:2] == ["flatpak-spawn", "--host"]


def test_in_the_flatpak_a_host_program_that_is_gone_is_stale(flatpak):
    _asked, state = flatpak
    state["rc"] = 1
    assert shortcuts._program_missing("/home/someone/Downloads/Nadare-b26.AppImage") is True


@pytest.mark.parametrize("rc, boom", [(2, False), (127, False), (0, True)])
def test_in_the_flatpak_no_answer_from_the_host_means_not_stale(flatpak, rc, boom):
    _asked, state = flatpak
    state["rc"], state["raise"] = rc, boom
    assert shortcuts._program_missing("/usr/bin/nadare") is False


def test_in_the_flatpak_a_visible_program_needs_no_host_call(tmp_path, flatpak):
    asked, _state = flatpak
    exe = tmp_path / "Nadare.AppImage"
    exe.write_text("")
    assert shortcuts._program_missing(str(exe)) is False
    assert asked == []


def test_outside_the_flatpak_the_host_is_never_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(
        shortcuts.subprocess, "run", lambda *a, **k: pytest.fail("no host call outside")
    )
    assert shortcuts._program_missing(str(tmp_path / "gone")) is True


def test_in_the_flatpak_a_working_entry_of_a_host_build_is_left_alone(tmp_path, flatpak):
    _asked, state = flatpak
    state["rc"] = 0
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", ["/home/someone/Downloads/Nadare-b27.AppImage"])
    before = entry.read_text()
    result = shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}},
        applications_dir=apps,
        argv=["flatpak", "run", shortcuts.FLATPAK_ID],
    )
    assert result["updated"] == []
    assert entry.read_text() == before


# ---- Steam start-up repair -------------------------------------------------------------
@pytest.fixture
def steam_closed(monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)


def test_a_broken_running_nadare_never_replaces_a_broken_steam_shortcut(
    tmp_path, monkeypatch, steam_closed
):
    stale = _entry(str(tmp_path / "gone" / "Nadare.AppImage"), 'launch -p "czn"')
    vdf = _write(tmp_path / "shortcuts.vdf", [stale])
    before = vdf.read_bytes()
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts(
        {"czn": {"title": "Game"}},
        argv=[str(tmp_path / "also-gone")],
        orphans_only=True,
        repair_stale=True,
    )
    assert vdf.read_bytes() == before


@pytest.mark.parametrize("exe", ["flatpak", "nadare"])
def test_a_bare_name_steam_exe_is_never_stale(tmp_path, monkeypatch, steam_closed, running, exe):
    kept = _entry(exe, f'run {shortcuts.FLATPAK_ID} launch -p "czn"')
    vdf = _write(tmp_path / "shortcuts.vdf", [kept])
    before = vdf.read_bytes()
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts(
        {"czn": {"title": "Game"}}, argv=running, orphans_only=True, repair_stale=True
    )
    assert vdf.read_bytes() == before


def test_a_quoted_steam_exe_with_odd_characters_that_exists_is_kept(
    tmp_path, monkeypatch, steam_closed, running
):
    exe = tmp_path / "My Games $x" / "Nadare.AppImage"
    exe.parent.mkdir()
    exe.write_text("")
    (entry,) = _read(_write(tmp_path / "probe.vdf", [_entry(str(exe), 'launch -p "czn"')]))
    assert shortcuts._program_missing(shortcuts._steam_exe(entry)) is False


# ---- refused launches that were still silent -------------------------------------------
class _NeedsNotice:
    account_notice = "This game signs in to your account."

    def requires_notice_ack(self) -> bool:
        return True

    def launch(self) -> int:
        raise AssertionError("must not start before the notice is accepted")


def test_an_unaccepted_account_notice_from_a_shortcut_notifies(
    tmp_path,
    monkeypatch,
    notes,
):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NeedsNotice())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 2  # exit code unchanged
    assert len(notes) == 1
    assert "notice" in notes[0][1].lower()


def test_an_unaccepted_account_notice_in_a_terminal_sends_nothing(
    tmp_path,
    monkeypatch,
    notes,
):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NeedsNotice())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: True)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 2
    assert notes == []


def test_an_unreadable_config_from_a_shortcut_notifies(tmp_path, monkeypatch, notes):
    folder = tmp_path / "config.json"
    folder.mkdir()  # exists, but cannot be read as a file
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(folder), "launch", "-p", "czn"]) == 1
    assert len(notes) == 1
    assert "czn" in notes[0][0]


def test_an_unreadable_config_for_another_command_does_not_notify(
    tmp_path,
    monkeypatch,
    notes,
):
    folder = tmp_path / "config.json"
    folder.mkdir()
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(folder), "status", "-p", "czn"]) == 1
    assert notes == []


def test_a_failing_notification_never_changes_the_exit_code(tmp_path, monkeypatch):
    from nadare.gui import notify

    def boom(*_a, **_k):
        raise RuntimeError("no bus")

    path = _config(tmp_path)
    monkeypatch.setattr(notify, "send", boom)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NotInstalled())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 1


# ---- the repair itself ------------------------------------------------------------------
def test_a_symlinked_entry_is_never_read(tmp_path, monkeypatch, running):
    apps = tmp_path / "apps"
    target = _menu_entry(tmp_path / "elsewhere", "czn", [str(tmp_path / "gone.AppImage")])
    apps.mkdir()
    link = apps / "nadare-czn.desktop"
    link.symlink_to(target)
    before = target.read_text()
    seen: list[Path] = []
    real = shortcuts._owned_profile
    monkeypatch.setattr(shortcuts, "_owned_profile", lambda p: seen.append(p) or real(p))
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert link not in seen
    assert target.read_text() == before and link.is_symlink()


def test_a_desktop_entry_is_repaired_and_stays_executable(tmp_path, running):
    apps, desk = tmp_path / "apps", tmp_path / "Desktop"
    apps.mkdir()
    desk.mkdir()
    entry = desk / "nadare-czn.desktop"
    entry.write_text(shortcuts.desktop_entry("czn", "Game", [str(tmp_path / "gone.AppImage")]))
    entry.chmod(0o755)
    result = shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, desktop_dir=desk, argv=running
    )
    assert result["updated"] == [entry]
    assert running[0] in entry.read_text()
    assert entry.stat().st_mode & 0o777 == 0o755


def test_a_bare_name_entry_is_never_stale(tmp_path, running):
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", ["nadare"])
    before = entry.read_text()
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert entry.read_text() == before


ODD = 'a\\b `c` 50%% "q" $HOME'


def test_odd_characters_round_trip_through_exec(tmp_path):
    exe = tmp_path / ODD / "Nadare.AppImage"
    text = shortcuts.desktop_entry("czn", "Game", [str(exe)])
    assert shortcuts._exec_program(text) == str(exe)


def test_a_missing_path_with_odd_characters_is_still_stale(tmp_path, running):
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", [str(tmp_path / ODD / "Nadare.AppImage")])  # never made
    result = shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert result["updated"] == [entry]


def test_exec_is_read_from_the_desktop_entry_group_only():
    text = (
        "[Desktop Action old]\nExec=/gone/old-nadare\n\n"
        "[Desktop Entry]\nType=Application\nExec=/opt/Nadare.AppImage launch -p czn\n"
    )
    assert shortcuts._exec_program(text) == "/opt/Nadare.AppImage"


@pytest.mark.parametrize(
    "line",
    ["Exec = /opt/N.AppImage launch", "Exec=/opt/N.AppImage\tlaunch", "Exec=/opt/N.AppImage"],
)
def test_exec_spacing_and_tabs_are_understood(line):
    assert shortcuts._exec_program(f"[Desktop Entry]\n{line}\n") == "/opt/N.AppImage"


def test_a_localised_exec_key_is_not_the_exec():
    text = "[Desktop Entry]\nExecutable=/x\nExec=/opt/N.AppImage launch\n"
    assert shortcuts._exec_program(text) == "/opt/N.AppImage"


# ---- desktop-entry values (brief #4) ----------------------------------------------------
def test_backslashes_in_keywords_and_comment_are_escaped():
    text = shortcuts.desktop_entry("a\\b;", "C:\\Games", ["/opt/N.AppImage"])
    lines = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    assert "a\\\\b\\;;" in lines["Keywords"]
    assert "C:\\\\Games" in lines["Comment"]
    assert not lines["Keywords"].endswith("\\;")  # the list still ends with a real separator


def test_a_short_name_with_a_dash_is_kept_whole_and_nadare_is_still_there():
    text = shortcuts.desktop_entry(
        "stella",
        "Stella Sora",
        ["/opt/N.AppImage"],
        profiles={"stella": {"backend": "stellasora-yostar", "title": "Stella Sora"}},
    )
    items = next(x for x in text.splitlines() if x.startswith("Keywords=")).split("=", 1)[1]
    assert items.split(";")[:-1] == ["stella-sora", "stella", "Nadare"]


# ---- the window and the tests' own isolation (brief #5) ---------------------------------
def test_the_window_skips_the_repair_while_the_config_has_a_notice(tmp_path, monkeypatch):
    from nadare.gui.main_window_parts import cleanup

    calls: list[object] = []
    monkeypatch.setattr(
        shortcuts, "repair_stale_desktop_entries", lambda *a, **k: calls.append(a) or {}
    )

    class _Win(cleanup.CleanupMixin):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            self.config = Config.load(tmp_path / "c.json")
            self.config.profiles["czn"] = {"title": "Game"}
            self.config.notice = "the settings file could not be read"

    _Win()._start_shortcut_repair(wait=True)
    assert calls == []


def test_unit_tests_never_start_the_real_shortcut_repair():
    """The thread reads HOME and runs xdg-user-dir: a stray one sees another test's HOME."""
    import conftest

    from nadare.gui.main_window import MainWindow
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    assert MainWindow._start_shortcut_repair is not CleanupMixin._start_shortcut_repair
    assert MainWindow._start_shortcut_repair(object()) is None
    assert "shortcut-repair" in conftest._DELETING_THREAD_NAMES  # it rewrites files: joined


def test_the_window_starts_the_repair_from_its_constructor():
    import inspect

    from nadare.gui.main_window import MainWindow

    assert "self._start_shortcut_repair()" in inspect.getsource(MainWindow.__init__)
