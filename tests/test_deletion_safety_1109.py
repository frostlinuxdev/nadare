# SPDX-License-Identifier: GPL-3.0-or-later
"""Deletion safety, 2026-11-09 audit: nothing Nadare did not make may be deleted or overwritten.

Each test names the finding it covers; reports in /mnt/kikyou/work/briefs/deletion-1109/.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from test_directdl_prune import make_zip, md5, server  # noqa: F401 - fixture reuse
from test_sophon import Build, plan_for, servers  # noqa: F401 - fixture reuse

from nadare.core import artwork, shortcuts, sophon, steamfix, steamglue
from nadare.core import directdl as dd


# ---------------------------------------------------------------------------
# artwork: the cover cache sweep converts and deletes only Nadare's own files
# ---------------------------------------------------------------------------
def _png_bytes() -> bytes:
    from test_artwork import _png_bytes as png

    return png()


def test_the_cover_migration_leaves_images_nadare_did_not_cache(tmp_path, monkeypatch):
    """A covers folder linked to a photo folder must not lose its photos to WebP."""
    photo = tmp_path / "holiday.png"
    photo.write_bytes(_png_bytes())
    ours = tmp_path / "game-cover.png"
    ours.write_bytes(_png_bytes())
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)

    assert artwork.migrate_cached_covers() == 1
    assert photo.read_bytes() == _png_bytes()
    assert not (tmp_path / "holiday.webp").exists()
    assert not ours.exists() and (tmp_path / "game-cover.webp").is_file()


def test_the_stale_part_sweep_only_removes_nadare_part_files(tmp_path):
    old = time.time() - 3 * 24 * 3600
    users = tmp_path / "video.part"
    users.write_bytes(b"half a download of something else")
    ours = tmp_path / f"game-cover.webp.{2**22 + 7}.{'a' * 32}.part"
    ours.write_bytes(b"x")
    for path in (users, ours):
        os.utime(path, (old, old))

    artwork._cleanup_stale_cache_parts(tmp_path)

    assert users.exists()
    assert not ours.exists()


# ---------------------------------------------------------------------------
# shortcuts: an empty profile list is no reason to delete every shortcut
# ---------------------------------------------------------------------------
def test_refreshing_desktop_entries_with_no_profiles_removes_nothing(tmp_path):
    apps = tmp_path / "applications"
    shortcuts.install_desktop_entry("game", "Game", applications_dir=apps, argv=["/n"])
    result = shortcuts.refresh_desktop_entries({}, applications_dir=apps, argv=["/n"])
    assert result["removed"] == []
    assert (apps / "nadare-game.desktop").exists()


def test_refreshing_steam_shortcuts_with_no_profiles_removes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: called.append(1) or [])
    result = shortcuts.refresh_steam_shortcuts({}, argv=["/n"])
    assert result["removed"] == []
    assert called == []  # not even opened


def test_the_cli_refresh_refuses_a_recovered_config(monkeypatch, capsys):
    from nadare import app

    class Recovered:
        notice = "config.json could not be read; started with an empty one"
        profiles: dict = {}

    removed = []
    monkeypatch.setattr(shortcuts, "refresh_desktop_entries", lambda *a, **k: removed.append(1))
    monkeypatch.setattr(shortcuts, "refresh_steam_shortcuts", lambda *a, **k: removed.append(1))
    code = app._shortcut_refresh(Recovered())
    assert code != 0
    assert removed == []
    assert "config" in capsys.readouterr().err.lower()


def test_a_hand_made_desktop_file_with_the_same_name_is_not_overwritten(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    mine = apps / "nadare-game.desktop"
    mine.write_text("[Desktop Entry]\nName=My own launcher\nExec=/my/script\n", encoding="utf-8")

    written = shortcuts.install_desktop_entry("game", "Game", applications_dir=apps, argv=["/n"])

    assert "My own launcher" in mine.read_text(encoding="utf-8")
    assert written and written[0] != mine and written[0].parent == apps


# ---------------------------------------------------------------------------
# Steam prefix backup and compat-tool links
# ---------------------------------------------------------------------------
def test_a_failed_prefix_backup_never_removes_a_folder_it_did_not_create(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    dest = tmp_path / "backups" / "pfx-backup"
    dest.parent.mkdir()
    # Something already sits at the old fixed staging name for this process.
    existing = dest.with_name(f".{dest.name}.partial-{os.getpid()}")
    existing.mkdir()
    (existing / "keep.txt").write_text("not Nadare's")
    monkeypatch.setattr(steamfix.diskspace, "check_space", lambda *a, **k: None)

    def broken_copy(src, dst, **kw):
        raise OSError("disk went away")

    monkeypatch.setattr(steamfix.shutil, "copytree", broken_copy)
    with pytest.raises(steamfix.BackendError):
        steamfix.make_backup(prefix, dest)
    assert (existing / "keep.txt").read_text() == "not Nadare's"


@pytest.mark.parametrize("tool", ["../x", "a/b", "..", "."])
def test_a_compat_tool_name_from_config_vdf_cannot_leave_the_tool_folder(tmp_path, tool):
    assert steamglue.remove_unused_tool(tmp_path, tool, '"InstallConfigStore" {}') is False


# ---------------------------------------------------------------------------
# directdl: another game's record in a shared folder is not "my older version"
# ---------------------------------------------------------------------------
def _archive(srv, name, blob, path, *, slot=""):
    return dd.Archive(
        name, dd.RemoteFile(f"{name}.zip", srv.url + path, len(blob), md5=md5(blob)), slot=slot
    )


def test_records_name_their_game(server, tmp_path):  # noqa: F811
    z = make_zip({"a.exe": b"a"})
    srv = server({"/a.zip": z})
    dd.sync(
        dd.Plan("gameA", "1", archives=(_archive(srv, "a-1", z, "/a.zip", slot="client"),)),
        tmp_path,
    )
    record = json.loads((tmp_path / dd.STATE_DIR / "a-1.done").read_text())
    assert record["game"] == "gameA"


def test_an_update_never_prunes_another_games_files_in_a_shared_folder(server, tmp_path):  # noqa: F811
    """Brown Dust 2 and Aether Gazer both use slot "client"; a folder kept after removing one
    game and reused for the other must not lose the first game's files."""
    other = make_zip({"other/data.bin": b"the other game"})
    a1 = make_zip({"a.exe": b"v1"})
    a2 = make_zip({"a.exe": b"v2"})
    srv = server({"/o.zip": other, "/a1.zip": a1, "/a2.zip": a2})
    dd.sync(
        dd.Plan("gameB", "9", archives=(_archive(srv, "b-9", other, "/o.zip", slot="client"),)),
        tmp_path,
    )
    dd.sync(
        dd.Plan("gameA", "1", archives=(_archive(srv, "a-1", a1, "/a1.zip", slot="client"),)),
        tmp_path,
    )
    dd.sync(
        dd.Plan("gameA", "2", archives=(_archive(srv, "a-2", a2, "/a2.zip", slot="client"),)),
        tmp_path,
    )

    assert (tmp_path / "other" / "data.bin").read_bytes() == b"the other game"
    assert (tmp_path / dd.STATE_DIR / "b-9.done").exists()


def test_a_legacy_record_from_another_host_is_not_claimed(server, tmp_path):  # noqa: F811
    """A record written before records named their game: claimed only from the same host."""
    theirs = make_zip({"theirs.bin": b"theirs"})
    a1 = make_zip({"a.exe": b"v1"})
    srv = server({"/t.zip": theirs, "/a1.zip": a1})
    dd.sync(dd.Plan("gameB", "7", archives=(_archive(srv, "t-7", theirs, "/t.zip"),)), tmp_path)
    state = tmp_path / dd.STATE_DIR
    record = json.loads((state / "t-7.done").read_text())
    # Make it look like an install from before this fix, from another publisher's CDN.
    record.pop("game", None)
    record.pop("slot", None)
    record["url"] = "https://cdn.other-publisher.example/client.zip"
    (state / "t-7.done").write_text(json.dumps(record))

    dd.sync(
        dd.Plan("gameA", "1", archives=(_archive(srv, "a-1", a1, "/a1.zip", slot="client"),)),
        tmp_path,
    )

    assert (tmp_path / "theirs.bin").read_bytes() == b"theirs"
    assert (state / "t-7.done").exists()


def test_the_member_list_is_never_written_through_a_planted_link(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    record = tmp_path / "root" / dd.STATE_DIR / "big.done"
    record.parent.mkdir(parents=True)
    sidecar = dd._member_list_path(record)
    sidecar.with_name(sidecar.name + ".tmp").symlink_to(outside)

    dd._write_member_list(record, ["a", "b"])

    assert outside.read_text() == "precious"
    assert sidecar.read_text().split() == ["a", "b"]


# ---------------------------------------------------------------------------
# Sophon: a stale file someone changed after the install is kept
# ---------------------------------------------------------------------------
def test_a_dropped_file_changed_after_the_install_is_kept(servers, tmp_path):  # noqa: F811
    old = Build({"keep.bin": b"k" * 100, "gone.bin": b"g" * 100}, chunk_size=100)
    servers(old)
    dd.sync(plan_for(old), tmp_path, workers=1)
    ledger = sophon._ledger_path(old.package(), tmp_path)
    later = ledger.stat().st_mtime + 60
    (tmp_path / "gone.bin").write_bytes(b"the user's edit")
    os.utime(tmp_path / "gone.bin", (later, later))

    new = Build({"keep.bin": b"k" * 100}, chunk_size=100)
    servers(new)
    dd.sync(plan_for(new), tmp_path, workers=1)

    assert (tmp_path / "gone.bin").read_bytes() == b"the user's edit"


def test_an_unchanged_dropped_file_is_still_removed(servers, tmp_path):  # noqa: F811
    old = Build({"keep.bin": b"k" * 100, "gone.bin": b"g" * 100}, chunk_size=100)
    servers(old)
    dd.sync(plan_for(old), tmp_path, workers=1)
    new = Build({"keep.bin": b"k" * 100}, chunk_size=100)
    servers(new)
    dd.sync(plan_for(new), tmp_path, workers=1)
    assert not (tmp_path / "gone.bin").exists()
