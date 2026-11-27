# SPDX-License-Identifier: GPL-3.0-or-later
"""#967 item 1: an archive whose name changes every version still prunes what it dropped.

Aether Gazer names its client zip after the version and an MD5, Brown Dust 2 after the version
(checked against the live APIs on 2026-11-08). The prune found an archive's previous record by
the archive's name only, so after an update the old record was never read (nothing pruned) and
stayed on disk owning its files forever. Aether Gazer's 63,000 asset files are named by their
content hash, so every changed asset left its old copy behind. ``Archive.slot`` names the role
that stays the same across versions.
"""

from __future__ import annotations

import json

import pytest
from test_directdl_prune import make_zip, md5, server  # noqa: F401 - fixture reuse

from nadare.core import directdl as dd


def _archive(srv, name, blob, path, *, slot=""):
    return dd.Archive(
        name,
        dd.RemoteFile(f"{name}.zip", srv.url + path, len(blob), md5=md5(blob)),
        slot=slot,
    )


def _state(root):
    return sorted(p.name for p in (root / dd.STATE_DIR).rglob("*.done"))


def test_a_renamed_client_archive_prunes_what_the_new_version_dropped(server, tmp_path):  # noqa: F811
    z1 = make_zip({"game.exe": b"v1", "Windows/aa/oldhash.ys": b"old asset"})
    z2 = make_zip({"game.exe": b"v2", "Windows/bb/newhash.ys": b"new asset"})
    srv = server({"/v1.zip": z1, "/v2.zip": z2})
    first = _archive(srv, "Game_prod_292_AAAA", z1, "/v1.zip", slot="client")
    second = _archive(srv, "Game_prod_293_BBBB", z2, "/v2.zip", slot="client")
    dd.sync(dd.Plan("T", "292", archives=(first,)), tmp_path)
    dd.sync(dd.Plan("T", "293", archives=(second,)), tmp_path)
    assert (tmp_path / "game.exe").read_bytes() == b"v2"
    assert (tmp_path / "Windows" / "bb" / "newhash.ys").is_file()
    assert not (tmp_path / "Windows" / "aa" / "oldhash.ys").exists()
    assert _state(tmp_path) == ["Game_prod_293_BBBB.done"]  # the old record is gone


def test_a_record_written_before_slots_counts_for_a_one_archive_plan(server, tmp_path):  # noqa: F811
    """Installs made before this fix have records without a slot."""
    z1 = make_zip({"game.exe": b"v1", "old.dll": b"gone"})
    z2 = make_zip({"game.exe": b"v2"})
    srv = server({"/v1.zip": z1, "/v2.zip": z2})
    dd.sync(dd.Plan("T", "1", archives=(_archive(srv, "g-1", z1, "/v1.zip"),)), tmp_path)
    record = tmp_path / dd.STATE_DIR / "g-1.done"
    assert "slot" not in json.loads(record.read_text())
    second = _archive(srv, "g-2", z2, "/v2.zip", slot="client")
    dd.sync(dd.Plan("T", "2", archives=(second,)), tmp_path)
    assert not (tmp_path / "old.dll").exists()
    assert _state(tmp_path) == ["g-2.done"]


def test_a_multi_archive_plan_never_treats_another_archive_as_an_old_version(server, tmp_path):  # noqa: F811
    """DLC: an archive installed beside the client keeps its files and its record."""
    client1 = make_zip({"game.exe": b"v1"})
    client2 = make_zip({"game.exe": b"v2"})
    dlc = make_zip({"dlc/voice.pck": b"voice"})
    srv = server({"/c1.zip": client1, "/c2.zip": client2, "/dlc.zip": dlc})
    dlc_archive = _archive(srv, "voice-pack", dlc, "/dlc.zip", slot="voice")
    dd.sync(
        dd.Plan("T", "1", archives=(_archive(srv, "c-1", client1, "/c1.zip", slot="client"),
                                    dlc_archive)),
        tmp_path,
    )
    dd.sync(
        dd.Plan("T", "2", archives=(_archive(srv, "c-2", client2, "/c2.zip", slot="client"),
                                    dlc_archive)),
        tmp_path,
    )
    assert (tmp_path / "dlc" / "voice.pck").read_bytes() == b"voice"
    assert "voice-pack.done" in _state(tmp_path)


def test_a_legacy_record_is_not_claimed_by_a_multi_archive_plan(server, tmp_path):  # noqa: F811
    old_dlc = make_zip({"dlc/old.pck": b"x"})
    client = make_zip({"game.exe": b"v1"})
    other = make_zip({"extra/a.bin": b"a"})
    srv = server({"/old.zip": old_dlc, "/c.zip": client, "/o.zip": other})
    dd.sync(dd.Plan("T", "0", archives=(_archive(srv, "old-dlc", old_dlc, "/old.zip"),)), tmp_path)
    dd.sync(
        dd.Plan("T", "1", archives=(_archive(srv, "c-1", client, "/c.zip", slot="client"),
                                    _archive(srv, "o", other, "/o.zip"))),
        tmp_path,
    )
    assert (tmp_path / "dlc" / "old.pck").read_bytes() == b"x"
    assert "old-dlc.done" in _state(tmp_path)


def test_a_dropped_file_the_user_changed_is_kept(server, tmp_path):  # noqa: F811
    z1 = make_zip({"game.exe": b"v1", "cfg.ini": b"default"})
    z2 = make_zip({"game.exe": b"v2"})
    srv = server({"/v1.zip": z1, "/v2.zip": z2})
    dd.sync(dd.Plan("T", "1", archives=(_archive(srv, "g-1", z1, "/v1.zip", slot="client"),)),
            tmp_path)
    (tmp_path / "cfg.ini").write_bytes(b"my settings")
    dd.sync(dd.Plan("T", "2", archives=(_archive(srv, "g-2", z2, "/v2.zip", slot="client"),)),
            tmp_path)
    assert (tmp_path / "cfg.ini").read_bytes() == b"my settings"


def test_a_failed_update_removes_nothing_and_keeps_the_old_record(server, tmp_path):  # noqa: F811
    z1 = make_zip({"game.exe": b"v1", "old.dll": b"gone"})
    srv = server({"/v1.zip": z1})
    dd.sync(dd.Plan("T", "1", archives=(_archive(srv, "g-1", z1, "/v1.zip", slot="client"),)),
            tmp_path)
    broken = dd.Archive(
        "g-2", dd.RemoteFile("g-2.zip", srv.url + "/missing.zip", 10, md5="0" * 32), slot="client"
    )
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "2", archives=(broken,)), tmp_path)
    assert (tmp_path / "old.dll").read_bytes() == b"gone"
    assert "g-1.done" in _state(tmp_path)


def test_the_live_sources_declare_their_client_slot():
    from nadare.core.sources import aethergazer, bd2

    ag = {
        "errorCode": "0",
        "data": {"gameUpdateInfo": {
            "currentVersion": "293",
            "downloadUrl": "https://x.example/293/pc/AG_293_B03AB844D2F9C0359577D7200D0FB67E.zip",
            "fileName": "AG_293_B03AB844D2F9C0359577D7200D0FB67E.zip",
        }},
    }
    plan = aethergazer.resolve(lambda url, headers=None: json.dumps(ag).encode())
    assert [a.slot for a in plan.archives] == ["client"]
    meta = {
        "download_url": "https://pc.example/g", "last_version": "20261030000",
        "exe_file": "BrownDust II.exe",
        "last_file": {"file": "/20261030000.tied.dat", "tied_crc": "1C2665F5"},
    }
    plan = bd2.resolve(lambda url, headers=None: json.dumps(meta).encode())
    assert [a.slot for a in plan.archives] == ["client"]


def test_endfield_declares_its_client_slot():
    """Endfield names its pack after the build (``Beyond_Release_v1d7-...``): without a slot the
    1.6.4 -> 1.7.2 update left the whole 1.6.4 VFS behind (test desktop, 11-25: 73 -> 124 GB)."""
    from nadare.core.sources import endfield

    url = (
        "https://cdn.example/1.7.2/packs/"
        "Beyond_Release_v1d7-Rel-os-11793826-1_prod_obt_official.zip.001"
    )
    payload = {
        "action": 1, "version": "1.7.2",
        "pkg": {"packs": [{"url": url, "package_size": "10", "md5": "0" * 32}]},
    }
    plan = endfield.resolve(lambda url, headers=None: json.dumps(payload).encode())
    assert [a.slot for a in plan.archives] == ["client"]


def test_an_install_updated_before_the_slot_is_cleaned_by_the_next_sync(server, tmp_path):  # noqa: F811
    """The test-desktop case: both versions installed without a slot, so the update kept the old
    record and its files. The next sync, with the slot and nothing to download, prunes them."""
    z1 = make_zip({"game.exe": b"v1", "VFS/shared.chk": b"same", "VFS/old.chk": b"old"})
    z2 = make_zip({"game.exe": b"v2", "VFS/shared.chk": b"same", "VFS/new.chk": b"new"})
    srv = server({"/v1.zip": z1, "/v2.zip": z2})
    dd.sync(dd.Plan("T", "1", archives=(_archive(srv, "g-1", z1, "/v1.zip"),)), tmp_path)
    dd.sync(dd.Plan("T", "2", archives=(_archive(srv, "g-2", z2, "/v2.zip"),)), tmp_path)
    assert (tmp_path / "VFS" / "old.chk").is_file()  # what the unslotted update left behind
    summary = dd.sync(
        dd.Plan("T", "2", archives=(_archive(srv, "g-2", z2, "/v2.zip", slot="client"),)),
        tmp_path,
    )
    assert summary["downloaded"] == 0
    assert not (tmp_path / "VFS" / "old.chk").exists()
    assert (tmp_path / "VFS" / "shared.chk").read_bytes() == b"same"
    assert (tmp_path / "VFS" / "new.chk").read_bytes() == b"new"
    assert _state(tmp_path) == ["g-2.done"]


def test_flx_pc_record_shapes_without_game_or_sequence_are_cleaned(server, tmp_path):  # noqa: F811
    """The real test-desktop state (#1822): the old record predates ``game`` and ``sequence``, the
    new one has them; neither has a slot. Only what the old record alone owns goes."""
    z1 = make_zip({"game.exe": b"v1", "VFS/shared.chk": b"same", "VFS/old.chk": b"old"})
    z2 = make_zip({"game.exe": b"v2", "VFS/shared.chk": b"same", "VFS/new.chk": b"new"})
    srv = server({"/v1.zip": z1, "/v2.zip": z2})
    dd.sync(dd.Plan("T", "1", archives=(_archive(srv, "g-1", z1, "/v1.zip"),)), tmp_path)
    old = tmp_path / dd.STATE_DIR / "g-1.done"
    record = json.loads(old.read_text())
    for key in ("game", "sequence", "slot"):
        record.pop(key, None)
    old.write_text(json.dumps(record))
    dd.sync(dd.Plan("T", "2", archives=(_archive(srv, "g-2", z2, "/v2.zip"),)), tmp_path)
    (tmp_path / "Saved.log").write_bytes(b"the game wrote this")
    dd.sync(dd.Plan("T", "2", archives=(_archive(srv, "g-2", z2, "/v2.zip", slot="client"),)),
            tmp_path)
    assert not (tmp_path / "VFS" / "old.chk").exists()
    assert (tmp_path / "VFS" / "shared.chk").read_bytes() == b"same"
    assert (tmp_path / "game.exe").read_bytes() == b"v2"
    assert (tmp_path / "Saved.log").is_file()
    assert _state(tmp_path) == ["g-2.done"]
