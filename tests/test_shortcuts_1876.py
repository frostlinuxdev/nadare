# SPDX-License-Identifier: GPL-3.0-or-later
"""#1876: shortcuts that stay working and say why when a game cannot start.

1. A launch refused before the game starts (not installed, update needed, a hold...) from a
   menu or Steam shortcut has no terminal: the reason is shown as a desktop notification.
2. A shortcut whose Nadare is gone (an AppImage replaced by a newer one, a removed checkout)
   is pointed at the running Nadare when the window opens; a working one is never touched.
3. The entry carries the game's short name as a search keyword (CZN, HSR...).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from test_steam_shortcut_ownership import _entry, _read, _write

from nadare import app, launchnotice
from nadare.core import shortcuts
from nadare.core.backends import BackendNotInstalled
from nadare.core.config import Config


# ---- 1. a refused launch is not silent -------------------------------------------------
class _NotInstalled:
    def requires_notice_ack(self) -> bool:
        return False

    def launch(self) -> int:
        raise BackendNotInstalled("Game files need to be acquired first", 1)


class _Plays:
    def requires_notice_ack(self) -> bool:
        return False

    def launch(self) -> int:
        return 0


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("czn", backend="wine", title="Chaos Zero Nightmare")
    cfg.save()
    return path


@pytest.fixture
def notes(monkeypatch):
    sent: list[tuple[str, str]] = []
    from nadare.gui import notify

    monkeypatch.setattr(notify, "send", lambda s, b="": sent.append((s, b)) or True)
    return sent


def test_a_refused_launch_without_a_terminal_sends_a_notification(
    tmp_path, monkeypatch, notes, capsys
):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NotInstalled())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 1
    assert len(notes) == 1
    summary, body = notes[0]
    assert "Chaos Zero Nightmare" in summary
    assert "nadare install -p czn" in body
    assert "nadare install -p czn" in capsys.readouterr().err  # the terminal text stays


def test_a_refused_launch_in_a_terminal_sends_nothing(tmp_path, monkeypatch, notes):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NotInstalled())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: True)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 1
    assert notes == []


def test_a_launch_that_plays_sends_nothing(tmp_path, monkeypatch, notes):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _Plays())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(path), "launch", "-p", "czn"]) == 0
    assert notes == []


def test_an_unknown_profile_from_a_stale_shortcut_says_so(tmp_path, monkeypatch, notes):
    path = _config(tmp_path)
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    assert app.run_cli(["-c", str(path), "launch", "-p", "gone"]) != 0
    assert len(notes) == 1
    assert "gone" in notes[0][0] + notes[0][1]


def test_other_commands_never_notify(tmp_path, monkeypatch, notes):
    path = _config(tmp_path)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _NotInstalled())
    monkeypatch.setattr(launchnotice, "has_terminal", lambda: False)
    app.run_cli(["-c", str(path), "stop", "-p", "czn"])
    assert notes == []


# ---- 2. stale shortcuts are repaired at start, working ones are not --------------------
@pytest.fixture
def running(tmp_path) -> list[str]:
    """How this Nadare starts: a file that exists, so only the entry decides."""
    exe = tmp_path / "running" / "Nadare-new.AppImage"
    exe.parent.mkdir()
    exe.write_text("")
    return [str(exe)]


def _menu_entry(apps: Path, profile: str, argv: list[str], title: str = "Game") -> Path:
    return shortcuts.install_desktop_entry(profile, title, applications_dir=apps, argv=argv)[0]


def test_an_entry_whose_appimage_is_gone_points_at_the_running_nadare(tmp_path):
    apps = tmp_path / "apps"
    old = tmp_path / "Nadare-0.1.0b26-x86_64.AppImage"  # never created: replaced by b27
    new = tmp_path / "Nadare-0.1.0b27-x86_64.AppImage"
    new.write_text("")
    entry = _menu_entry(apps, "czn", [str(old)], "Chaos Zero Nightmare")
    result = shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Chaos Zero Nightmare"}}, applications_dir=apps, argv=[str(new)]
    )
    assert result["updated"] == [entry]
    assert f"Exec={new} launch -p czn" in entry.read_text()


def test_an_entry_of_a_removed_checkout_is_repaired(tmp_path):
    apps = tmp_path / "apps"
    venv_python = tmp_path / "nadare-b29" / ".venv" / "bin" / "python"  # folder removed
    entry = _menu_entry(apps, "czn", [str(venv_python), "-m", "nadare"])
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=["flatpak", "run", "x.y.Z"]
    )
    assert "Exec=flatpak run x.y.Z launch -p czn" in entry.read_text()


def test_a_working_entry_of_another_build_is_left_alone(tmp_path, running):
    apps = tmp_path / "apps"
    other = tmp_path / "Nadare-0.1.0b27-x86_64.AppImage"
    other.write_text("")
    entry = _menu_entry(apps, "czn", [str(other)])
    before = entry.read_text()
    result = shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert result["updated"] == []
    assert entry.read_text() == before


def test_flatpak_and_path_entries_are_never_stale(tmp_path, running):
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", ["flatpak", "run", shortcuts.FLATPAK_ID])
    before = entry.read_text()
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert entry.read_text() == before


def test_repair_keeps_entries_of_removed_games_and_foreign_files(tmp_path, running):
    apps = tmp_path / "apps"
    gone = _menu_entry(apps, "gone", [str(tmp_path / "missing.AppImage")])
    foreign = apps / "nadare-notours.desktop"
    foreign.write_text("[Desktop Entry]\nExec=/missing/x\n")
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert gone.exists() and "missing.AppImage" in gone.read_text()  # not its job to delete
    assert foreign.read_text() == "[Desktop Entry]\nExec=/missing/x\n"


def test_a_broken_running_nadare_never_replaces_a_broken_entry(tmp_path):
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", [str(tmp_path / "old.AppImage")])
    before = entry.read_text()
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=[str(tmp_path / "also-gone")]
    )
    assert entry.read_text() == before


def test_repair_with_an_empty_config_does_nothing(tmp_path, running):
    apps = tmp_path / "apps"
    entry = _menu_entry(apps, "czn", [str(tmp_path / "missing.AppImage")])
    before = entry.read_text()
    shortcuts.repair_stale_desktop_entries({}, applications_dir=apps, argv=running)
    assert entry.read_text() == before


def test_a_path_with_spaces_and_quotes_is_read_back(tmp_path, running):
    apps = tmp_path / "apps"
    odd = tmp_path / 'My "Apps" $dir' / "Nadare.AppImage"
    odd.parent.mkdir()
    odd.write_text("")
    entry = _menu_entry(apps, "czn", [str(odd)])
    before = entry.read_text()
    shortcuts.repair_stale_desktop_entries(
        {"czn": {"title": "Game"}}, applications_dir=apps, argv=running
    )
    assert entry.read_text() == before  # it exists, so it is not stale


@pytest.fixture
def steam_closed(monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)


def test_a_steam_shortcut_whose_nadare_is_gone_is_repaired(tmp_path, monkeypatch, steam_closed):
    stale = _entry(str(tmp_path / "gone" / "python3"), '-m nadare launch -p "czn"', title="CZN")
    vdf = _write(tmp_path / "shortcuts.vdf", [stale])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    new = tmp_path / "Nadare.AppImage"
    new.write_text("")
    shortcuts.refresh_steam_shortcuts(
        {"czn": {"title": "Chaos Zero Nightmare"}},
        argv=[str(new)],
        orphans_only=True,
        repair_stale=True,
    )
    (entry,) = _read(vdf)
    assert entry["Exe"] == f'"{new}"'
    assert entry["LaunchOptions"] == 'launch -p "czn"'  # no "-m nadare" left over
    assert entry["AppName"] == "CZN"  # titles are not the start-up job


def test_a_working_steam_shortcut_is_left_alone(tmp_path, monkeypatch, steam_closed):
    exe = tmp_path / "Nadare-b27.AppImage"
    exe.write_text("")
    kept = _entry(str(exe), 'launch -p "czn"')
    vdf = _write(tmp_path / "shortcuts.vdf", [kept])
    before = vdf.read_bytes()
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts(
        {"czn": {"title": "Game"}}, argv=["/dev/python"], orphans_only=True, repair_stale=True
    )
    assert vdf.read_bytes() == before


def test_an_explicit_refresh_also_fixes_the_launch_options(tmp_path, monkeypatch, steam_closed):
    old = _entry("/usr/bin/python3", '-m nadare launch -p "czn"')
    vdf = _write(tmp_path / "shortcuts.vdf", [old])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts({"czn": {"title": "Game"}}, argv=["/opt/Nadare.AppImage"])
    (entry,) = _read(vdf)
    assert entry["Exe"] == '"/opt/Nadare.AppImage"'
    assert entry["LaunchOptions"] == 'launch -p "czn"'


# ---- 3. search keywords ---------------------------------------------------------------
def test_the_entry_carries_the_short_name_as_a_keyword():
    text = shortcuts.desktop_entry(
        "czn",
        "Chaos Zero Nightmare",
        ["/opt/N.AppImage"],
        profiles={"czn": {"backend": "stove-czn", "title": "Chaos Zero Nightmare"}},
    )
    keywords = next(line for line in text.splitlines() if line.startswith("Keywords="))
    assert "czn;" in keywords
    assert "Nadare;" in keywords
    assert keywords.endswith(";")
    assert "Comment=" in text


def test_a_profile_key_with_a_semicolon_cannot_split_the_keywords():
    text = shortcuts.desktop_entry("a;b", "Game", ["/opt/N.AppImage"])
    keywords = next(line for line in text.splitlines() if line.startswith("Keywords="))
    assert "a\\;b;" in keywords


def test_a_short_name_with_a_dash_is_also_searchable_as_words():
    text = shortcuts.desktop_entry(
        "stella",
        "Stella Sora",
        ["/opt/N.AppImage"],
        profiles={"stella": {"backend": "stellasora-yostar", "title": "Stella Sora"}},
    )
    keywords = next(line for line in text.splitlines() if line.startswith("Keywords="))
    assert "stella-sora;" in keywords


# ---- the start-up wiring --------------------------------------------------------------
def test_the_window_repairs_stale_menu_entries_at_start(tmp_path, monkeypatch):
    from nadare.gui.main_window_parts import cleanup

    calls: list[dict] = []
    monkeypatch.setattr(
        shortcuts,
        "repair_stale_desktop_entries",
        lambda profiles, **k: calls.append(dict(profiles)) or {"updated": [], "warnings": []},
    )

    class _Win(cleanup.CleanupMixin):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            self.config = Config.load(tmp_path / "c.json")
            self.config.profiles["czn"] = {"title": "Game"}

    win = _Win()
    win._start_shortcut_repair(wait=True)
    assert calls == [{"czn": {"title": "Game"}}]


def test_the_window_skips_the_repair_with_an_unreadable_config(tmp_path, monkeypatch):
    from nadare.gui.main_window_parts import cleanup

    calls: list[object] = []
    monkeypatch.setattr(
        shortcuts, "repair_stale_desktop_entries", lambda *a, **k: calls.append(a) or {}
    )

    class _Win(cleanup.CleanupMixin):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            self.config = Config.load(tmp_path / "c.json")  # no profiles

    _Win()._start_shortcut_repair(wait=True)
    assert calls == []


def test_the_added_message_says_some_launchers_need_a_restart(tmp_path, monkeypatch):
    from nadare.gui.main_window_parts import shortcuts as gui_shortcuts

    monkeypatch.setattr(shortcuts, "install_desktop_entry", lambda *a, **k: [tmp_path / "x"])
    config = Config.load(tmp_path / "c.json")
    config.profiles["czn"] = {"title": "Chaos Zero Nightmare"}
    ok, text = gui_shortcuts.apply_shortcut(config, "czn", "menu-add")
    assert ok
    assert "Chaos Zero Nightmare is in the app menu" in text
    assert "restart" in text and "log in again" in text
