# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: files an earlier update left behind are removed when Kuro's own lists retire them."""

from __future__ import annotations

import json

import pytest

from nadare.core import directdl, kuropatch

ROOT = "https://cdn.example/"


def _plan(entries):
    return directdl.Plan(
        game_tag="wuwa", version="3.8.0",
        files=(
            directdl.RemoteFile("Client/Content/Paks/a.pak", "https://x/a", 1, "0" * 32),
            directdl.RemoteFile("Client/Content/HD/b.pak", "https://x/b", 1, "0" * 32),
        ),
        extra={kuropatch.EXTRA_KEY: {"root": ROOT, "packs": {"common": entries}}},
    )


def _entry(version):
    return {"version": version, "indexFile": f"p/{version}.json", "baseUrl": f"p/{version}"}


@pytest.fixture()
def disk(tmp_path):
    root = tmp_path / "install"
    for rel in (
        "Client/Content/Paks/a.pak", "Client/Content/HD/b.pak",
        "Client/Content/Paks/old1.pak", "Client/Content/HD/old2.pak",
        "Client/Content/Paks/~mods/mymod.pak", "Client/Saved/Resources/x.dat",
        "Client/Content/Paks/unknown.pak",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    (root / directdl.STATE_DIR).mkdir()
    (root / directdl.STATE_DIR / "state.json").write_text("{}")
    return root


def _fetcher(lists, seen):
    def fetch(url, headers=None):
        seen.append(url)
        version = url.rsplit("/", 1)[1].removesuffix(".json")
        return json.dumps({"deleteFiles": lists.get(version, [])}).encode()

    return fetch


def test_only_files_kuro_retired_are_found(disk):
    seen: list[str] = []
    fetch = _fetcher({"3.7.2": ["Client/Content/Paks/old1.pak"],
                      "3.7.1": ["Client/Content/HD/old2.pak"]}, seen)
    plan = _plan([_entry("3.7.1"), _entry("3.7.2"), _entry("3.6.0")])
    found = kuropatch.retired_on_disk(plan, disk, fetch)
    assert sorted(found) == ["Client/Content/HD/old2.pak", "Client/Content/Paks/old1.pak"]
    # Newest lists first; it stops once every leftover is explained or the lists run out.
    assert seen[:2] == [f"{ROOT}p/3.7.2.json", f"{ROOT}p/3.7.1.json"]


def test_mods_saved_data_state_and_unexplained_files_stay(disk):
    fetch = _fetcher({"3.7.2": [
        "Client/Content/Paks/~mods/mymod.pak", "Client/Saved/Resources/x.dat",
        f"{directdl.STATE_DIR}/state.json",
    ]}, [])
    found = kuropatch.retired_on_disk(plan=_plan([_entry("3.7.2")]), root=disk, fetch=fetch)
    assert found == ()


def test_nothing_left_over_fetches_nothing(tmp_path):
    root = tmp_path / "install"
    (root / "Client/Content/Paks").mkdir(parents=True)
    (root / "Client/Content/Paks/a.pak").write_bytes(b"x")
    seen: list[str] = []
    assert kuropatch.retired_on_disk(_plan([_entry("3.7.2")]), root, _fetcher({}, seen)) == ()
    assert seen == []


def test_at_most_a_few_lists_are_read(disk):
    seen: list[str] = []
    entries = [_entry(f"3.{minor}.0") for minor in range(20)]
    kuropatch.retired_on_disk(_plan(entries), disk, _fetcher({}, seen))
    assert len(seen) == kuropatch.MAX_CLEANUP_LISTS


def test_a_broken_list_is_skipped(disk):
    def fetch(url, headers=None):
        if "3.7.2" in url:
            raise directdl.DirectError("404")
        return json.dumps({"deleteFiles": ["Client/Content/Paks/old1.pak"]}).encode()

    found = kuropatch.retired_on_disk(_plan([_entry("3.7.2"), _entry("3.7.1")]), disk, fetch)
    assert found == ("Client/Content/Paks/old1.pak",)


def test_a_game_without_kuro_lists_finds_nothing(disk):
    plan = directdl.Plan(game_tag="bd2", version="2", files=())
    assert kuropatch.retired_on_disk(plan, disk, _fetcher({}, [])) == ()


def test_linked_folders_are_not_walked(disk, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.pak").write_bytes(b"precious")
    (disk / "Client/Content/Linked").symlink_to(outside)
    fetch = _fetcher({"3.7.2": ["Client/Content/Linked/victim.pak"]}, [])
    assert kuropatch.retired_on_disk(_plan([_entry("3.7.2")]), disk, fetch) == ()
