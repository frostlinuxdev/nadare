# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Steam client helpers: process gate, commands, config.vdf edits."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nadare.core import steamtools
from nadare.core.backends import BackendError

VDF = (
    '"InstallConfigStore"\n'
    "{\n"
    '\t"Software"\n'
    "\t{\n"
    '\t\t"Valve"\n'
    "\t\t{\n"
    '\t\t\t"Steam"\n'
    "\t\t\t{\n"
    '\t\t\t\t"CompatToolMapping"\n'
    "\t\t\t\t{\n"
    '\t\t\t\t\t"5019180"\n'
    "\t\t\t\t\t{\n"
    '\t\t\t\t\t\t"name"\t\t"proton_experimental"\n'
    '\t\t\t\t\t\t"config"\t\t""\n'
    '\t\t\t\t\t\t"Priority"\t\t"250"\n'
    "\t\t\t\t\t}\n"
    '\t\t\t\t\t"3347400"\n'
    "\t\t\t\t\t{\n"
    '\t\t\t\t\t\t"name"\t\t"proton_9"\n'
    '\t\t\t\t\t\t"config"\t\t""\n'
    '\t\t\t\t\t\t"Priority"\t\t"250"\n'
    "\t\t\t\t\t}\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)


def _proc_pid(proc: Path, pid: int, argv: list[str]) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


# ---- Steam running / commands -----------------------------------------
def test_steam_running_detects_client_and_helpers(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 100, ["/usr/bin/steam", "-silent"])
    assert steamtools.steam_running(proc_root=proc) is True
    assert steamtools.steam_pids(proc_root=proc) == [100]


def test_steam_running_is_false_without_a_client(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 101, ["reaper", "SteamLaunch", "AppId=5019180"])
    _proc_pid(proc, 102, ["wineserver"])
    assert steamtools.steam_running(proc_root=proc) is False
    assert steamtools.steam_running(proc_root=tmp_path / "missing") is False


def test_steam_running_survives_vanishing_entries(tmp_path):
    proc = tmp_path / "proc"
    (proc / "103").mkdir(parents=True)
    assert steamtools.steam_running(proc_root=proc) is False


def test_shutdown_and_start_commands():
    assert steamtools.shutdown_command() == ["steam", "-shutdown"]
    assert steamtools.start_command() == ["steam"]
    assert steamtools.restart_plan() == [["steam", "-shutdown"], ["steam"]]


def test_flatpak_binary_is_split_for_every_command():
    flatpak = "flatpak run com.valvesoftware.Steam"
    assert steamtools.shutdown_command(flatpak) == [
        "flatpak",
        "run",
        "com.valvesoftware.Steam",
        "-shutdown",
    ]
    assert steamtools.unlockh264_command(flatpak) == [
        "flatpak",
        "run",
        "com.valvesoftware.Steam",
        "steam://unlockh264/",
    ]


def test_bad_binary_is_a_clear_error():
    with pytest.raises(BackendError):
        steamtools.steam_command('"unterminated')


# ---- H.264 -------------------------------------------------------------
def test_unlockh264_command_default():
    assert steamtools.unlockh264_command() == ["steam", "steam://unlockh264/"]


def test_h264_game_list_is_reported_data_not_code():
    games = {g.game: g for g in steamtools.H264_GAMES}
    assert games["Uma Musume"].status == "reported"
    assert games["Uma Musume"].symptom == "white screen"
    assert games["Snowbreak"].status == "reported"
    for game in steamtools.H264_GAMES:
        assert game.recommended == ("dwproton", "ge")


def test_h264_game_ids_are_the_ones_on_the_steam_store():
    """Looked up on the public store pages (both are free to play) on 2026-10-31."""
    games = {g.game: g for g in steamtools.H264_GAMES}
    assert games["Uma Musume"].app_id == "3224770"  # Umamusume: Pretty Derby
    assert games["Snowbreak"].app_id == "2668080"  # Snowbreak: Containment Zone
    assert steamtools.h264_game("3224770") is games["Uma Musume"]
    assert steamtools.h264_game(2668080) is games["Snowbreak"]
    assert steamtools.h264_game("3347400") is None  # GFL2 is a prefix-fixer game, not H.264


def test_h264_lookup_by_name_and_empty_id():
    assert steamtools.h264_game("Uma Musume") is not None
    assert steamtools.h264_game("uma musume") is not None
    assert steamtools.h264_game("Snowbreak") is not None
    assert steamtools.h264_game("") is None
    assert steamtools.h264_game("Half-Life") is None


# ---- config.vdf reading ------------------------------------------------
def test_reads_the_current_entry():
    entry = steamtools.read_compat_entry(VDF, "5019180")
    assert entry is not None
    assert entry.tool == "proton_experimental"
    assert entry.fields["Priority"] == "250"
    assert entry.app_id == "5019180"


def test_missing_entry_and_missing_section_are_none():
    assert steamtools.read_compat_entry(VDF, "9999999") is None
    no_section = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t}\n}\n'
    assert steamtools.read_compat_entry(no_section, "5019180") is None


def test_nested_quotes_and_comment_like_text_are_not_structure():
    text = VDF.replace(
        '"config"\t\t""\n\t\t\t\t\t\t"Priority"\t\t"250"\n'
        "\t\t\t\t\t}\n",
        '"config"\t\t"a \\"quoted\\" { brace } // not a comment"\n'
        '\t\t\t\t\t\t"Priority"\t\t"250"\n'
        "\t\t\t\t\t}\n",
        1,
    )
    entry = steamtools.read_compat_entry(text, "5019180")
    assert entry is not None
    assert entry.fields["config"] == 'a "quoted" { brace } // not a comment'
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    assert steamtools.read_compat_entry(edited, "5019180").tool == "dwproton"
    assert (
        steamtools.read_compat_entry(edited, "5019180").fields["config"]
        == 'a "quoted" { brace } // not a comment'
    )


# ---- config.vdf editing -------------------------------------------------
def test_edit_changes_only_the_tool_line():
    edited = steamtools.edit_compat_text(VDF, "5019180", "dwproton")
    before = VDF.splitlines(keepends=True)
    after = edited.splitlines(keepends=True)
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    assert after[changed[0]] == '\t\t\t\t\t\t"name"\t\t"dwproton"\n'
    assert steamtools.read_compat_entry(edited, "3347400").tool == "proton_9"


def test_edit_keeps_tabs_spacing_and_trailing_text_byte_for_byte():
    text = VDF + '"Trailing"\n{\n\t"x" "y"\n}\n'
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    assert edited.startswith(VDF[: VDF.index('"proton_experimental"')])
    tail = text[text.index('"proton_experimental"') + len('"proton_experimental"') :]
    assert edited.endswith(tail)


def test_edit_creates_the_mapping_section_when_missing():
    text = (
        '"InstallConfigStore"\n'
        "{\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Valve"\n'
        "\t\t{\n"
        '\t\t\t"Steam"\n'
        "\t\t\t{\n"
        '\t\t\t\t"Other"\t\t"kept"\n'
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    entry = steamtools.read_compat_entry(edited, "5019180")
    assert entry is not None and entry.tool == "dwproton"
    expected = text.replace(
        '\t\t\t\t"Other"\t\t"kept"\n',
        '\t\t\t\t"Other"\t\t"kept"\n'
        '\t\t\t\t"CompatToolMapping"\n'
        "\t\t\t\t{\n"
        '\t\t\t\t\t"5019180"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"name"\t\t"dwproton"\n'
        '\t\t\t\t\t\t"config"\t\t""\n'
        '\t\t\t\t\t\t"Priority"\t\t"250"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n",
    )
    assert edited == expected


def test_edit_inserts_an_entry_for_an_app_without_one():
    text = VDF.replace(
        '\t\t\t\t\t"3347400"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"name"\t\t"proton_9"\n'
        '\t\t\t\t\t\t"config"\t\t""\n'
        '\t\t\t\t\t\t"Priority"\t\t"250"\n'
        "\t\t\t\t\t}\n",
        "",
    )
    edited = steamtools.edit_compat_text(text, "3347400", "dwproton")
    assert steamtools.read_compat_entry(edited, "3347400").tool == "dwproton"
    assert steamtools.read_compat_entry(edited, "5019180").tool == "proton_experimental"
    # Removing the new entry gives back the original bytes.
    previous = steamtools.read_compat_entry(edited, "3347400")
    assert steamtools.restore_compat_text(edited, "3347400", None) == text
    assert previous is not None


def test_edit_inserts_a_missing_name_field_only():
    text = VDF.replace(
        '\t\t\t\t\t\t"name"\t\t"proton_experimental"\n', ""
    )
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    entry = steamtools.read_compat_entry(edited, "5019180")
    assert entry.tool == "dwproton"
    assert entry.fields["Priority"] == "250"
    # Only the inserted name line is new; removing it restores the file exactly.
    assert edited.replace('\t\t\t\t\t\t"name"\t\t"dwproton"\n', "", 1) == text


def test_duplicate_entries_refuse_to_guess():
    text = VDF.replace(
        "\t\t\t\t\t}\n\t\t\t\t}\n",
        "\t\t\t\t\t}\n"
        '\t\t\t\t\t"5019180"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"name"\t\t"gw_proton"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n",
        1,
    )
    with pytest.raises(BackendError) as info:
        steamtools.edit_compat_text(text, "5019180", "dwproton")
    assert "duplicate" in info.value.message.lower()


def test_crlf_line_endings_are_preserved():
    text = VDF.replace("\n", "\r\n")
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    assert edited.count("\n") == edited.count("\r\n")
    assert steamtools.read_compat_entry(edited, "5019180").tool == "dwproton"


def test_restore_brings_back_the_exact_previous_entry():
    previous = steamtools.read_compat_entry(VDF, "5019180")
    edited = steamtools.edit_compat_text(VDF, "5019180", "dwproton")
    restored = steamtools.restore_compat_text(edited, "5019180", previous)
    assert restored == VDF


def test_very_large_file_is_edited_correctly_and_quickly():
    entries = "".join(
        f'\t\t\t\t\t"{1000000 + i}"\n'
        "\t\t\t\t\t{\n"
        f'\t\t\t\t\t\t"name"\t\t"tool_{i}"\n'
        "\t\t\t\t\t}\n"
        for i in range(20000)
    )
    text = VDF.replace(
        "\t\t\t\t}\n\t\t\t}\n",
        entries + "\t\t\t\t}\n\t\t\t}\n",
        1,
    )
    started = time.perf_counter()
    edited = steamtools.edit_compat_text(text, "5019180", "dwproton")
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0
    assert steamtools.read_compat_entry(edited, "5019180").tool == "dwproton"
    assert steamtools.read_compat_entry(edited, "1019999").tool == "tool_19999"


# ---- config.vdf writing / backups --------------------------------------
def _write(tmp_path: Path, text: str = VDF) -> Path:
    path = tmp_path / "config" / "config.vdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode())
    return path


def test_apply_writes_a_dated_backup_first(tmp_path):
    path = _write(tmp_path)
    result = steamtools.apply_compat_tool(
        path, "5019180", "dwproton", today="2026-10-31", steam_running_fn=lambda: False
    )
    assert result.changed is True
    assert result.backup == path.with_name("config.vdf.bak-2026-10-31")
    assert result.backup.read_bytes() == VDF.encode()
    assert steamtools.read_compat_entry(path.read_text(), "5019180").tool == "dwproton"


def test_apply_does_not_overwrite_an_existing_daily_backup(tmp_path):
    path = _write(tmp_path)
    existing = path.with_name("config.vdf.bak-2026-10-31")
    existing.write_text("older backup\n")
    steamtools.apply_compat_tool(
        path, "5019180", "dwproton", today="2026-10-31", steam_running_fn=lambda: False
    )
    assert existing.read_text() == "older backup\n"
    assert path.with_name("config.vdf.bak-2026-10-31-2").exists()


def test_apply_refuses_while_steam_is_running(tmp_path):
    path = _write(tmp_path)
    with pytest.raises(BackendError) as info:
        steamtools.apply_compat_tool(
            path, "5019180", "dwproton", steam_running_fn=lambda: True
        )
    assert "steam" in info.value.message.lower()
    assert path.read_text() == VDF
    assert not list(path.parent.glob("config.vdf.bak-*"))


def test_apply_with_the_same_tool_changes_nothing(tmp_path):
    path = _write(tmp_path)
    result = steamtools.apply_compat_tool(
        path, "5019180", "proton_experimental", steam_running_fn=lambda: False
    )
    assert result.changed is False
    assert result.backup is None
    assert path.read_text() == VDF


def test_restore_writes_a_backup_and_the_previous_entry(tmp_path):
    path = _write(tmp_path)
    previous = steamtools.read_compat_entry(VDF, "5019180")
    steamtools.apply_compat_tool(
        path, "5019180", "dwproton", today="2026-10-31", steam_running_fn=lambda: False
    )
    result = steamtools.restore_compat_tool(
        path,
        "5019180",
        previous,
        today="2026-10-31",
        steam_running_fn=lambda: False,
    )
    assert result.changed is True
    assert path.read_text() == VDF
    assert result.backup is not None and result.backup.exists()


def test_apply_missing_config_vdf_is_a_clear_error(tmp_path):
    with pytest.raises(BackendError) as info:
        steamtools.apply_compat_tool(
            tmp_path / "nope" / "config.vdf",
            "5019180",
            "dwproton",
            steam_running_fn=lambda: False,
        )
    assert "config.vdf" in info.value.message


# ---- scrutiny fixes: cut-off files, symlinked config, durability, dangling backup names ----------
CUT_OFF = (
    '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
    '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"5019180"\n\t\t\t\t\t{\n'
    '\t\t\t\t\t\t"name"\t\t"proton_experimental"\n'
)


def test_a_cut_off_entry_is_refused_instead_of_edited_and_never_undone():
    with pytest.raises(BackendError, match="not closed"):
        steamtools.edit_compat_text(CUT_OFF, "5019180", "dwproton")


def test_a_symlinked_config_stays_a_symlink_and_its_target_is_edited(tmp_path):
    target = tmp_path / "dotfiles" / "config.vdf"
    target.parent.mkdir()
    target.write_text(VDF)
    link = tmp_path / "Steam" / "config" / "config.vdf"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    steamtools.apply_compat_tool(link, "5019180", "dwproton", steam_running_fn=lambda: False)
    assert link.is_symlink() and link.resolve() == target.resolve()
    assert steamtools.read_compat_entry(target.read_text(), "5019180").tool == "dwproton"
    assert list(link.parent.glob("config.vdf.bak-*"))  # the backup sits next to the link


def test_the_edited_file_is_flushed_to_disk_before_it_replaces_the_original(tmp_path, monkeypatch):
    synced: list[int] = []
    real = steamtools.os.fsync
    monkeypatch.setattr(steamtools.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    steamtools.apply_compat_tool(path, "5019180", "dwproton", steam_running_fn=lambda: False)
    assert len(synced) >= 2  # the new file, and the folder that now names it


def test_a_dangling_backup_name_is_never_written_through(tmp_path):
    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    elsewhere = tmp_path / "elsewhere"
    stamp = "2026-10-31"
    (tmp_path / f"config.vdf.bak-{stamp}").symlink_to(elsewhere)  # dangling
    steamtools.apply_compat_tool(
        path, "5019180", "dwproton", steam_running_fn=lambda: False, today=stamp
    )
    assert not elsewhere.exists()
    assert (tmp_path / f"config.vdf.bak-{stamp}-2").read_text() == VDF


def test_a_flatpak_steam_is_seen_as_running(tmp_path):
    proc = tmp_path / "proc"
    _proc_pid(proc, 200, ["/usr/bin/flatpak", "run", "com.valvesoftware.Steam"])
    _proc_pid(proc, 201, ["/usr/bin/flatpak", "run", "org.mozilla.firefox"])
    assert steamtools.steam_pids(proc_root=proc) == [200]


LOGINUSERS = (
    '"users"\n{\n\t"76561198000000040"\n\t{\n\t\t"AccountName"\t\t"old"\n'
    '\t\t"MostRecent"\t\t"0"\n\t}\n\t"76561198000000042"\n\t{\n'
    '\t\t"AccountName"\t\t"now"\n\t\t"MostRecent"\t\t"1"\n\t}\n}\n'
)


def test_the_most_recent_account_is_read_from_loginusers():
    assert steamtools.most_recent_user(LOGINUSERS) == "76561198000000042"
    assert steamtools.most_recent_user(LOGINUSERS.replace('"1"', '"0"')) is None
    assert steamtools.most_recent_user("") is None
    assert steamtools.most_recent_user('"users"\n{\n}\n') is None


def test_a_steam_id_becomes_the_folder_number_of_its_account():
    assert steamtools.account_id("76561198000000042") == 76561198000000042 - 76561197960265728
    assert steamtools.account_id("junk") is None
    assert steamtools.account_id("123") is None  # below the individual-account range


# ---- wave 2: undo guard, duplicates, backup races, two processes, fsync order -------------
def test_undoing_a_cut_off_entry_is_refused_too():
    previous = steamtools.read_compat_entry(VDF, "5019180")
    for earlier in (None, previous):
        with pytest.raises(BackendError, match="not closed"):
            steamtools.restore_compat_text(CUT_OFF, "5019180", earlier)


def test_an_entry_with_two_name_fields_is_refused_not_half_edited():
    doubled = VDF.replace(
        '\t\t\t\t\t\t"name"\t\t"proton_experimental"\n',
        '\t\t\t\t\t\t"name"\t\t"ge_proton"\n\t\t\t\t\t\t"name"\t\t"proton_experimental"\n',
        1,
    )
    with pytest.raises(BackendError, match="more than one"):
        steamtools.edit_compat_text(doubled, "5019180", "dwproton")


def test_a_backup_name_that_looks_free_is_still_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    stamp = "2026-10-31"
    taken = tmp_path / f"config.vdf.bak-{stamp}"
    taken.write_text("the first change's restore point")
    monkeypatch.setattr(Path, "exists", lambda self: False)  # another process won the race
    steamtools.apply_compat_tool(
        path, "5019180", "dwproton", steam_running_fn=lambda: False, today=stamp
    )
    monkeypatch.undo()
    assert taken.read_text() == "the first change's restore point"
    assert (tmp_path / f"config.vdf.bak-{stamp}-2").read_text() == VDF


def test_two_changes_to_one_config_take_turns_across_processes(tmp_path):
    import threading

    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    locks = tmp_path / "locks"
    finished = threading.Event()

    def other_process() -> None:
        steamtools.apply_compat_tool(
            path, "3347400", "dwproton", steam_running_fn=lambda: False, lock_dir=locks
        )
        finished.set()

    with steamtools.config_lock(path, lock_dir=locks):
        thread = threading.Thread(target=other_process)
        thread.start()
        assert not finished.wait(0.4)  # it waits for whoever holds the lock
    assert finished.wait(5)
    thread.join()
    text = path.read_text()
    assert steamtools.read_compat_entry(text, "3347400").tool == "dwproton"


def test_the_edit_is_read_again_after_the_lock_is_taken(tmp_path):
    import threading

    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    locks = tmp_path / "locks"
    done = threading.Event()

    def second() -> None:
        steamtools.apply_compat_tool(
            path, "3347400", "dwproton", steam_running_fn=lambda: False, lock_dir=locks
        )
        done.set()

    with steamtools.config_lock(path, lock_dir=locks):
        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.2)
        # the lock holder (the "first process") changes the file while the second one waits
        path.write_text(steamtools.edit_compat_text(VDF, "5019180", "dwproton"))
    assert done.wait(5)
    thread.join()
    text = path.read_text()
    assert steamtools.read_compat_entry(text, "5019180").tool == "dwproton"  # not reverted
    assert steamtools.read_compat_entry(text, "3347400").tool == "dwproton"


def test_the_file_is_flushed_before_it_replaces_the_original_in_that_order(tmp_path, monkeypatch):
    events: list[str] = []
    real_fsync, real_replace = steamtools.os.fsync, steamtools.os.replace
    monkeypatch.setattr(steamtools.os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(
        steamtools.os, "replace", lambda a, b: events.append("replace") or real_replace(a, b)
    )
    path = tmp_path / "config.vdf"
    path.write_text(VDF)
    steamtools.apply_compat_tool(path, "5019180", "dwproton", steam_running_fn=lambda: False)
    assert events[-3:] == ["fsync", "replace", "fsync"]  # file, rename, then the folder
