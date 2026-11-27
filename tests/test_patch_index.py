# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare index patch specs: validation, offline cache read, and best-effort refresh."""

from __future__ import annotations

import copy
import json
import logging

import pytest

from nadare.core import indexfetch, patchspecs

INDEX_BASE = "https://index.invalid/"
KIBOTL = patchspecs.KIBOTL_EN
VALID_URL = "https://github.com/KiboTL/text/releases/download/v1.1-3219598/en.zip"
NEW_ARCHIVE_SHA = "1" * 64
NEW_FILE_SHA = "2" * 64


def _valid_doc(**overrides):
    doc = {
        "schema": 1,
        "id": KIBOTL.id,
        "kind": "azurpromilia-jp-yostar",
        "version": "v1.1-3219598",
        "url": VALID_URL,
        "archive_sha256": NEW_ARCHIVE_SHA,
        "archive_size": 123456,
        "files": [
            {
                "member": item.member,
                "dest": item.dest,
                "sha256": f"{i:064x}" if i else NEW_FILE_SHA,
                "size": item.size + 10,
                "before_sha256": item.before_sha256,
                "before_size": item.before_size,
            }
            for i, item in enumerate(KIBOTL.files)
        ],
    }
    doc.update(overrides)
    return doc


def _write_cached_index(doc, base: str = INDEX_BASE, rel: str = "patches/kibotl-en.json") -> None:
    path = indexfetch.cache_path(base, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_valid_index_doc_overrides_builtin_pins_and_keeps_builtin_metadata():
    doc = _valid_doc(
        title="Hijacked Title",
        notes="Hijacked Notes",
        requires=["other-patch"],
    )
    spec = patchspecs.spec_from_index(KIBOTL, doc)
    assert spec is not None
    assert spec.id == KIBOTL.id
    assert spec.version == "v1.1-3219598"
    assert spec.url == VALID_URL
    assert spec.archive_sha256 == NEW_ARCHIVE_SHA
    assert spec.archive_size == 123456
    assert len(spec.files) == len(KIBOTL.files)
    assert spec.files[0].sha256 == NEW_FILE_SHA
    assert spec.files[0].size == KIBOTL.files[0].size + 10
    # Metadata always comes from the built-in spec, never the index.
    assert spec.title == KIBOTL.title
    assert spec.notes == KIBOTL.notes
    assert spec.requires == KIBOTL.requires


def _mutate_first_file(field: str, value):
    doc = _valid_doc()
    if value is _DELETE:
        doc["files"][0].pop(field, None)
    else:
        doc["files"][0][field] = value
    return doc


_DELETE = object()


def _duplicate_dest_doc():
    doc = _valid_doc()
    doc["files"][1]["dest"] = doc["files"][0]["dest"]
    return doc


def _duplicate_member_doc():
    doc = _valid_doc()
    doc["files"][1]["member"] = doc["files"][0]["member"]
    return doc


def _too_many_files_doc():
    doc = _valid_doc()
    base = doc["files"][0]
    doc["files"] = [
        {
            **copy.deepcopy(base),
            "member": f"file{i}.bytes",
            "dest": f"AzurPromiliaJP_Data/bundle{i}.r",
        }
        for i in range(17)
    ]
    return doc


REJECTION_CASES = [
    ("not_a_dict", ["not", "a", "dict"]),
    ("schema_2", _valid_doc(schema=2)),
    ("schema_bool", _valid_doc(schema=True)),
    ("wrong_id", _valid_doc(id="other-patch")),
    ("wrong_kind", _valid_doc(kind="other-game")),
    ("http_url", _valid_doc(url="http://github.com/KiboTL/text/releases/download/v1/en.zip")),
    ("other_host", _valid_doc(url="https://evil.invalid/KiboTL/text/releases/download/v1/en.zip")),
    (
        "wrong_repo_prefix",
        _valid_doc(url="https://github.com/Other/text/releases/download/v1/en.zip"),
    ),
    (
        "url_userinfo",
        _valid_doc(url="https://user:pass@github.com/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_userinfo_host_spoof",
        _valid_doc(
            url="https://github.com@evil.invalid/KiboTL/text/releases/download/v1/en.zip"
        ),
    ),
    (
        "url_port_99999",
        _valid_doc(url="https://github.com:99999/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_port_abc",
        _valid_doc(url="https://github.com:abc/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_bad_ipv6",
        _valid_doc(url="https://[github.com/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_dotdot_segment",
        _valid_doc(url="https://github.com/KiboTL/text/releases/download/../other/en.zip"),
    ),
    (
        "url_encoded_dotdot",
        _valid_doc(url="https://github.com/KiboTL/text/releases/download/%2e%2e/other/en.zip"),
    ),
    (
        "url_query",
        _valid_doc(url="https://github.com/KiboTL/text/releases/download/v1/en.zip?x=1"),
    ),
    (
        "url_empty_query",
        _valid_doc(url="https://github.com/KiboTL/text/releases/download/v1/en.zip?"),
    ),
    (
        "url_fragment",
        _valid_doc(url="https://github.com/KiboTL/text/releases/download/v1/en.zip#top"),
    ),
    (
        "url_port",
        _valid_doc(url="https://github.com:8443/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_empty_port",
        _valid_doc(url="https://github.com:/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "url_explicit_443",
        _valid_doc(url="https://github.com:443/KiboTL/text/releases/download/v1/en.zip"),
    ),
    (
        "dest_outside_root",
        _mutate_first_file("dest", "OtherLongDirectoryName/outside_root_file.r"),
    ),
    ("dest_traversal", _mutate_first_file("dest", "AzurPromiliaJP_Data/../escape.r")),
    ("dest_not_r", _mutate_first_file("dest", "AzurPromiliaJP_Data/file.bytes")),
    ("dest_ending_slash_dot_r", _mutate_first_file("dest", "AzurPromiliaJP_Data/sub/.r")),
    ("member_with_slash", _mutate_first_file("member", "sub/vstring.bytes")),
    ("member_exe", _mutate_first_file("member", "KiboTL.exe")),
    ("member_dotdot", _mutate_first_file("member", "a..b.bytes")),
    ("missing_before_sha256", _mutate_first_file("before_sha256", _DELETE)),
    ("none_before_sha256", _mutate_first_file("before_sha256", None)),
    ("missing_before_size", _mutate_first_file("before_size", _DELETE)),
    ("none_before_size", _mutate_first_file("before_size", None)),
    ("negative_before_size", _mutate_first_file("before_size", -1)),
    ("bool_before_size", _mutate_first_file("before_size", True)),
    ("bad_archive_sha", _valid_doc(archive_sha256="not-a-sha")),
    ("archive_sha_trailing_newline", _valid_doc(archive_sha256="1" * 64 + "\n")),
    ("bad_file_sha", _mutate_first_file("sha256", "ABCDEF" + "0" * 58)),
    ("file_sha_trailing_newline", _mutate_first_file("sha256", "2" * 64 + "\n")),
    ("before_sha_trailing_newline", _mutate_first_file("before_sha256", "3" * 64 + "\n")),
    ("huge_archive_size", _valid_doc(archive_size=(1 << 30) + 1)),
    ("zero_archive_size", _valid_doc(archive_size=0)),
    ("bool_archive_size", _valid_doc(archive_size=True)),
    ("negative_file_size", _mutate_first_file("size", -1)),
    ("duplicate_dest", _duplicate_dest_doc()),
    ("duplicate_member", _duplicate_member_doc()),
    ("too_many_files", _too_many_files_doc()),
    ("empty_files", _valid_doc(files=[])),
    ("empty_version", _valid_doc(version="")),
    ("long_version", _valid_doc(version="v" * 80)),
    ("version_with_slash", _valid_doc(version="v1/../../evil")),
    ("version_with_newline", _valid_doc(version="v1\n2")),
]


@pytest.mark.parametrize(("name", "bad_doc"), REJECTION_CASES, ids=[c[0] for c in REJECTION_CASES])
def test_bad_index_doc_is_rejected_with_warning_and_falls_back_to_builtin(
    name, bad_doc, monkeypatch, caplog
):
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)
    with caplog.at_level(logging.WARNING, logger="nadare.core.patchspecs"):
        assert patchspecs.spec_from_index(KIBOTL, bad_doc) is None
    assert caplog.records, f"expected a warning log for {name}"
    _write_cached_index(bad_doc)
    assert patchspecs.specs_for("azurpromilia-jp-yostar") == (KIBOTL,)


def test_specs_for_reads_valid_cache_without_network(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)
    _write_cached_index(_valid_doc())
    monkeypatch.setattr(
        indexfetch, "fetch_json", lambda *a, **k: pytest.fail("specs_for must not call fetch_json")
    )
    monkeypatch.setattr(
        indexfetch, "open_bytes", lambda *a, **k: pytest.fail("specs_for must not touch network")
    )

    specs = patchspecs.specs_for("azurpromilia-jp-yostar")
    assert len(specs) == 1
    assert specs[0].archive_sha256 == NEW_ARCHIVE_SHA
    assert specs[0].version == "v1.1-3219598"
    assert patchspecs.spec_by_id("azurpromilia-jp-yostar", "kibotl-en") == specs[0]


def test_refresh_fetches_caches_and_returns_updated_spec(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)
    doc = _valid_doc()
    seen_urls = []

    def fake_open(url, timeout=10, max_bytes=65536):
        seen_urls.append(url)
        return json.dumps(doc).encode("utf-8")

    monkeypatch.setattr(indexfetch, "open_bytes", fake_open)
    refreshed = patchspecs.refresh("azurpromilia-jp-yostar")
    assert seen_urls == [f"{INDEX_BASE}patches/kibotl-en.json"]
    assert len(refreshed) == 1
    assert refreshed[0].archive_sha256 == NEW_ARCHIVE_SHA
    # Subsequent offline specs_for sees the cached index spec.
    monkeypatch.setattr(
        indexfetch, "open_bytes", lambda *a, **k: pytest.fail("specs_for must not touch network")
    )
    assert patchspecs.specs_for("azurpromilia-jp-yostar") == refreshed


def test_refresh_survives_index_unavailable_and_keeps_cached_or_builtin(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)
    monkeypatch.setattr(
        indexfetch,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(indexfetch.IndexUnavailable("offline")),
    )
    # No cache -> built-in fallback, no exception.
    assert patchspecs.refresh("azurpromilia-jp-yostar") == (KIBOTL,)

    # With a valid cached doc -> keeps what specs_for gives.
    _write_cached_index(_valid_doc())
    result = patchspecs.refresh("azurpromilia-jp-yostar")
    assert len(result) == 1
    assert result[0].archive_sha256 == NEW_ARCHIVE_SHA


def test_is_builtin_fallback_identifies_only_the_shipped_objects(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)
    assert patchspecs.is_builtin_fallback(KIBOTL) is True

    # With no cache, specs_for returns the built-in object itself.
    fallback = patchspecs.specs_for("azurpromilia-jp-yostar")[0]
    assert fallback is KIBOTL
    assert patchspecs.is_builtin_fallback(fallback) is True

    # An index doc pinning the EXACT same hashes as KIBOTL builds a new object -> not a fallback.
    same_as_builtin = {
        "schema": 1,
        "id": KIBOTL.id,
        "kind": "azurpromilia-jp-yostar",
        "version": KIBOTL.version,
        "url": KIBOTL.url,
        "archive_sha256": KIBOTL.archive_sha256,
        "archive_size": 123456,
        "files": [
            {
                "member": item.member,
                "dest": item.dest,
                "sha256": item.sha256,
                "size": item.size,
                "before_sha256": item.before_sha256,
                "before_size": item.before_size,
            }
            for item in KIBOTL.files
        ],
    }
    from_doc = patchspecs.spec_from_index(KIBOTL, same_as_builtin)
    assert from_doc is not None
    assert from_doc.archive_sha256 == KIBOTL.archive_sha256
    assert patchspecs.is_builtin_fallback(from_doc) is False

    _write_cached_index(same_as_builtin)
    cached = patchspecs.specs_for("azurpromilia-jp-yostar")[0]
    assert cached.archive_sha256 == KIBOTL.archive_sha256
    assert patchspecs.is_builtin_fallback(cached) is False
