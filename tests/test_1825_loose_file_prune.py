# SPDX-License-Identifier: GPL-3.0-or-later
"""#1825: games installed as loose files remove the files a new version no longer lists.

A plan with ``prune_files`` keeps a ledger of its files (path, size, md5) in ``.nadare``. The next
successful sync removes a file the old ledger listed and the new plan does not, through the same
guarded prune as archive members: only a plain file still at its old size and md5 that nothing
else owns. A plan without the flag (Kuro tiers) never records or removes anything this way.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from _httpserver import Server

from nadare.core import directdl as dd


@pytest.fixture
def serve():
    made = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _plan(server, version, contents, *, prune=True):
    files = tuple(
        dd.RemoteFile(
            path,
            f"{server.url}/{version}/{path}",
            len(data),
            md5=hashlib.md5(data).hexdigest(),
        )
        for path, data in contents.items()
    )
    return dd.Plan("loose", version, files=files, prune_files=prune)


V1 = {"game.exe": b"MZ v1", "Data/old.pak": b"old pack" * 10, "Data/keep.pak": b"keep" * 10}
V2 = {"game.exe": b"MZ v2!", "Data/keep.pak": b"keep" * 10, "Data/new.pak": b"new" * 10}


def _serve_both(serve):
    return serve({f"/{v}/{p}": d for v, c in (("1", V1), ("2", V2)) for p, d in c.items()})


def test_a_file_the_new_version_drops_is_removed(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert not (tmp_path / "Data/old.pak").exists()
    assert (tmp_path / "Data/keep.pak").read_bytes() == V2["Data/keep.pak"]
    assert (tmp_path / "Data/new.pak").is_file()


def test_the_ledger_follows_the_installed_version(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    ledger = json.loads((tmp_path / dd.STATE_DIR / "files-loose.json").read_text())
    assert ledger["game"] == "loose" and ledger["version"] == "2"
    assert sorted(ledger["files"]) == sorted(V2)


def test_a_file_the_user_changed_is_kept(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    (tmp_path / "Data/old.pak").write_bytes(b"OLD PACK" * 10)  # same size, other bytes
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").read_bytes() == b"OLD PACK" * 10


def test_files_the_game_wrote_are_never_candidates(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    (tmp_path / "Data/Saved.log").write_text("the game's own log")
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert (tmp_path / "Data/Saved.log").is_file()


def test_an_install_without_a_ledger_only_starts_one(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1, prune=False), tmp_path, workers=1)
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()  # nothing said it was ours
    assert (tmp_path / dd.STATE_DIR / "files-loose.json").is_file()


def test_a_plan_without_the_flag_neither_records_nor_removes(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1, prune=False), tmp_path, workers=1)
    dd.sync(_plan(s, "2", V2, prune=False), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()
    assert not (tmp_path / dd.STATE_DIR / "files-loose.json").exists()


def test_a_case_only_rename_is_not_a_drop(serve, tmp_path):
    s = serve({"/1/Data/old.pak": V1["Data/old.pak"], "/2/data/OLD.pak": V1["Data/old.pak"]})
    dd.sync(_plan(s, "1", {"Data/old.pak": V1["Data/old.pak"]}), tmp_path, workers=1)
    dd.sync(_plan(s, "2", {"data/OLD.pak": V1["Data/old.pak"]}), tmp_path, workers=1)
    # On a case-insensitive drive both names are one file: it must survive.
    assert (tmp_path / "Data/old.pak").is_file()


def test_a_file_an_archive_record_owns_is_kept(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    record = {"members": ["Data/old.pak"], "member_sizes": {}, "member_crc32": {}}
    (tmp_path / dd.STATE_DIR / "dlc.zip.done").write_text(json.dumps(record))
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()


def test_a_ledger_of_another_game_is_ignored(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    path = tmp_path / dd.STATE_DIR / "files-loose.json"
    ledger = json.loads(path.read_text())
    ledger["game"] = "other"
    path.write_text(json.dumps(ledger))
    dd.sync(_plan(s, "2", V2), tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()


def test_a_failed_sync_removes_nothing_and_keeps_the_old_ledger(serve, tmp_path):
    s = _serve_both(serve)
    dd.sync(_plan(s, "1", V1), tmp_path, workers=1)
    broken = dd.RemoteFile("Data/gone.pak", s.url + "/missing", 5, md5="0" * 32)
    plan = _plan(s, "2", V2)
    plan = dd.Plan("loose", "2", files=(*plan.files, broken), prune_files=True)
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path, workers=1)
    assert (tmp_path / "Data/old.pak").is_file()
    ledger = json.loads((tmp_path / dd.STATE_DIR / "files-loose.json").read_text())
    assert ledger["version"] == "1"


# ---- the loose-file games opt in --------------------------------------------------------------
def _snowbreak():
    from test_snowbreak_source import _manifest, _resolve

    return _resolve(_manifest())


def _gfl2():
    from test_catalog_sources import FakeFetch, fx

    from nadare.core.sources import gfl2

    cdn = "https://gf2-us-cdn.sunborngame.com/prod/"
    flex = "https://gf2-launcher-us.sunborngame.com/flexible_config"
    ver_url = cdn + "AssetBundles_Windows/28279/Version.txt"
    return gfl2.resolve(
        FakeFetch({flex: fx("gfl2_flexible_config.json"), ver_url: fx("gfl2_Version.txt")})
    )


def _tof():
    from test_tof_source import resolve

    return resolve()[0]


@pytest.mark.parametrize("make", [_snowbreak, _gfl2, _tof], ids=["snowbreak", "gfl2", "tof"])
def test_the_loose_file_games_keep_a_ledger(make):
    assert make().prune_files


def test_nte_keeps_a_ledger():
    from test_nte_source import FakeFetch, routes

    from nadare.core.sources import nte

    assert nte.resolve(FakeFetch(routes())).prune_files
