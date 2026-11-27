# SPDX-License-Identifier: GPL-3.0-or-later
"""#1825: NIKKE without its launcher removes the files a new manifest no longer lists.

``railrepo.install(..., ledger=name)`` records what it installed in ``.nadare/files-<name>.json``
and, after a complete install, removes a file the previous record listed and the new manifest
does not (directdl's guarded prune: same size and md5, a plain file, owned by nothing else).
Without ``ledger`` nothing is recorded or removed (the install of a folder Nadare does not own).
"""

from __future__ import annotations

import json

from _rail_fixtures import Repo

from nadare.core import directdl, railrepo
from nadare.core.sources import nikke as source

V1 = {
    "NIKKE\\game\\nikke.exe": b"MZ v1",
    "NIKKE\\game\\old.dll": b"old" * 20,
    "NIKKE\\game\\keep.dat": b"keep" * 9,
}
V2 = {
    "NIKKE\\game\\nikke.exe": b"MZ v2!",
    "NIKKE\\game\\keep.dat": b"keep" * 9,
    "NIKKE\\game\\new.dat": b"new" * 7,
}


def _install(files, dest, **kwargs):
    repo = Repo(files)
    return railrepo.install(
        railrepo.repositories(repo.version_info()), dest, fetch=repo.fetch, **kwargs
    )


def test_a_file_the_new_manifest_drops_is_removed(tmp_path):
    _install(V1, tmp_path, ledger="nikke-client")
    assert (tmp_path / "NIKKE/game/old.dll").is_file()
    _install(V2, tmp_path, ledger="nikke-client")
    assert not (tmp_path / "NIKKE/game/old.dll").exists()
    assert (tmp_path / "NIKKE/game/new.dat").read_bytes() == b"new" * 7


def test_files_the_game_downloaded_itself_are_kept(tmp_path):
    _install(V1, tmp_path, ledger="nikke-client")
    resources = tmp_path / "NIKKE/game/nikke_Data/StreamingAssets/res.bundle"
    resources.parent.mkdir(parents=True)
    resources.write_bytes(b"resource the game fetched")
    _install(V2, tmp_path, ledger="nikke-client")
    assert resources.is_file()


def test_a_file_the_launcher_updated_since_is_kept(tmp_path):
    _install(V1, tmp_path, ledger="nikke-client")
    (tmp_path / "NIKKE/game/old.dll").write_bytes(b"OLD" * 20)  # the launcher's newer copy
    _install(V2, tmp_path, ledger="nikke-client")
    assert (tmp_path / "NIKKE/game/old.dll").read_bytes() == b"OLD" * 20


def test_a_path_another_ledger_lists_is_kept(tmp_path):
    _install(V1, tmp_path, ledger="nikke-client")
    _install({"NIKKE\\game\\old.dll": b"old" * 20}, tmp_path, ledger="other")
    _install(V2, tmp_path, ledger="nikke-client")
    assert (tmp_path / "NIKKE/game/old.dll").is_file()


def test_without_a_ledger_nothing_is_recorded_or_removed(tmp_path):
    _install(V1, tmp_path)
    _install(V2, tmp_path)
    assert (tmp_path / "NIKKE/game/old.dll").is_file()
    assert not (tmp_path / directdl.STATE_DIR).exists()


def test_the_ledger_lists_the_installed_files(tmp_path):
    _install(V1, tmp_path, ledger="nikke-client")
    ledger = json.loads((tmp_path / directdl.STATE_DIR / "files-nikke-client.json").read_text())
    assert sorted(ledger["files"]) == [
        "NIKKE/game/keep.dat",
        "NIKKE/game/nikke.exe",
        "NIKKE/game/old.dll",
    ]


def test_the_nikke_installs_keep_separate_ledgers(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(source.railrepo, "install", lambda *a, **k: seen.append(k.get("ledger")))
    monkeypatch.setattr(source, "launcher_info", lambda post=None: {})
    monkeypatch.setattr(source, "launcher_repositories", lambda info: [])
    monkeypatch.setattr(source, "client_info", lambda post=None: {})
    monkeypatch.setattr(source, "game_repositories", lambda info: [])
    source.install_launcher(tmp_path)
    source.install_game(tmp_path)
    assert seen == ["nikke-launcher", "nikke-client"]
