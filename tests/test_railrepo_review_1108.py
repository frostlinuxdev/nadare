# SPDX-License-Identifier: GPL-3.0-or-later
"""Malformed-input findings of the 2026-11-08 railrepo review (each verified before the fix)."""

from __future__ import annotations

import pytest
from _rail_fixtures import Repo

from nadare.core import railrepo
from nadare.core.events import Cancel, Cancelled
from nadare.core.sources import nikke as source

CDN = "https://cloud.nikke-kr.com/PC/prod/rid.50-r.test"


def test_a_negative_size_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(source.download, "http_open", lambda *a, **k: pytest.fail("requested"))
    with pytest.raises(railrepo.RailError):
        source.fetch_object(CDN + "/x.wgc", -2, "00" * 16)


def test_a_negative_manifest_size_in_the_api_record_is_refused():
    info = Repo({"a.txt": b"x"}, cdn_root=CDN).version_info()
    import json

    files = json.loads(info["cos_repo_files"])
    files[0]["manifest_files"][0]["manifest_size"] = -2
    info["cos_repo_files"] = json.dumps(files)
    with pytest.raises(railrepo.RailError):
        railrepo.repositories(info)


def test_field_reading_stops_at_its_limit():
    with pytest.raises(railrepo.RailError):
        railrepo._read_fields(b"\x6a\x00" * 50_000, limit=1000)
    assert len(railrepo._read_fields(b"\x6a\x00" * 10, limit=1000)) == 10


def test_a_manifest_with_too_many_entries_is_refused(monkeypatch):
    repo = Repo({f"f{i}.txt": b"x" for i in range(40)})
    (entry,) = railrepo.repositories(repo.version_info())
    monkeypatch.setattr(railrepo, "_MAX_FILES", 10)
    with pytest.raises(railrepo.RailError):
        railrepo.parse_manifest(repo.manifest, entry)


@pytest.mark.parametrize(
    "files",
    [{"Foo": b"x", "foo\\bar": b"y"}, {"data\\Foo": b"x", "Data\\foo\\b.txt": b"y"},
     {"a\\b": b"x", "A\\B\\c": b"y"}],
)
def test_a_file_and_a_folder_of_the_same_name_are_refused(tmp_path, files):
    repo = Repo(files)
    with pytest.raises(railrepo.RailError):
        repos = railrepo.repositories(repo.version_info())
        railrepo.install(repos, tmp_path / "L", fetch=repo.fetch)


def test_a_folder_entry_and_files_inside_it_are_fine(tmp_path):
    repo = Repo({"tpf_ui": None, "tpf_ui\\a.png": b"x", "TPF_UI\\b.png": b"y"})
    with pytest.raises(railrepo.RailError):
        # different case for the same folder is still a collision on Windows
        repos = railrepo.repositories(repo.version_info())
        railrepo.install(repos, tmp_path / "L", fetch=repo.fetch)
    ok = Repo({"tpf_ui": None, "tpf_ui\\a.png": b"x", "tpf_ui\\b.png": b"y"})
    railrepo.install(railrepo.repositories(ok.version_info()), tmp_path / "M", fetch=ok.fetch)


def test_cancel_before_anything_is_downloaded_is_honoured(tmp_path):
    repo = Repo({"a.txt": b"abc"})
    repos = railrepo.repositories(repo.version_info())
    railrepo.install(repos, tmp_path / "L", fetch=repo.fetch)  # everything is now kept
    cancel = Cancel()

    def fetch(url, size, md5_hex):
        cancel.cancel()  # the user cancels while the manifest is fetched
        return repo.fetch(url, size, md5_hex)

    with pytest.raises(Cancelled):
        railrepo.install(repos, tmp_path / "L", fetch=fetch, cancel=cancel)


def test_cancel_between_kept_files_is_honoured(tmp_path, monkeypatch):
    repo = Repo({f"f{i}.txt": bytes([i]) * 10 for i in range(5)})
    repos = railrepo.repositories(repo.version_info())
    railrepo.install(repos, tmp_path / "L", fetch=repo.fetch)
    cancel = Cancel()
    seen = []
    real = railrepo._keep_existing

    def keep(path, size, md5):
        seen.append(path)
        if len(seen) == 2:
            cancel.cancel()
        return real(path, size, md5)

    monkeypatch.setattr(railrepo, "_keep_existing", keep)
    with pytest.raises(Cancelled):
        railrepo.install(repos, tmp_path / "L", fetch=repo.fetch, cancel=cancel)
    assert len(seen) < 5

