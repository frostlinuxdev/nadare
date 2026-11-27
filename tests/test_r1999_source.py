# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved publisher responses for Reverse: 1999."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import r1999

FIX = Path(__file__).parent / "fixtures/catalog/r1999_resource.json"
SAME = FIX.with_name("r1999_same.json")


class Post:
    def __init__(self, resource=None, version=None):
        self.resource = json.loads(FIX.read_bytes()) if resource is None else resource
        self.default_resource = resource is None
        self.version = (
            json.loads(FIX.with_name("r1999_version.json").read_bytes())
            if version is None else version
        )
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body), headers))
        if (
            url.endswith("/resource")
            and self.default_resource
            and json.loads(body)["currentVersion"]
        ):
            self.resource = json.loads(FIX.with_name("r1999_diff.json").read_bytes())
        return json.dumps(self.version if url.endswith("/version") else self.resource).encode()


@pytest.mark.parametrize(
    "current,env,literal",
    [
        ("", 4, "60001_2_4_200_6001_"),
        ("3.9.0", 4, "60001_2_4_200_6001_3.9.0"),
        ("3.9.0", 3, "60001_2_3_200_6001_3.9.0"),
    ],
)
def test_signed_requests(current, env, literal):
    post = Post()
    r1999.resolve(post, current_version=current, env_type=env)
    expected = hashlib.md5(literal.encode()).hexdigest()
    if current == "":
        assert expected == "ba1831fafb35243e0610c7288cc065ab"
    base = {
        "gameId": "60001",
        "osType": 2,
        "envType": env,
        "channelId": 200,
        "subChannelId": "6001",
        "currentVersion": current,
        "sign": expected,
    }
    assert post.calls == [
        (
            "https://hotupdate-hw.sl916.com/diff-update/version",
            base,
            {"lang": "en", "Content-Type": "application/json"},
        ),
        (
            "https://hotupdate-hw.sl916.com/diff-update/resource",
            dict(base, targetVersion="3.9.1"),
            {"lang": "en", "Content-Type": "application/json"},
        ),
    ]


@pytest.mark.parametrize(
    "current,key,count", [("", "fullPackage", 3), ("3.9.0", "diffPackage", 1)]
)
def test_plan(current, key, count):
    post = Post()
    plan = r1999.resolve(post, current_version=current)
    package = post.resource["data"][key]
    assert (plan.game_tag, plan.version, plan.exe) == ("r1999", "3.9.1", "reverse1999.exe")
    assert plan.files == () and len(plan.archives) == count
    assert plan.extra["installed_size"] == package["packageRealSize"]
    assert plan.extra["download_size"] == sum(a.source.size for a in plan.archives)
    for archive, url in zip(plan.archives, package["packageUrls"], strict=True):
        assert archive.source.url == url
        assert archive.unwrap == ("7z",) and archive.parts == ()
        assert archive.stream is False  # streaming cannot enforce the redirect host guard
        metadata = re.search(r"size_(\d+)_md5_([0-9a-f]{32})_\d+\.7z$", url)
        assert archive.source.size == int(metadata[1])
        assert archive.source.md5 == metadata[2]
        dd.safe_relative_path(archive.source.path)
    assert plan.redirect_allowed("https://download.bluepoch.com/en/game/a.7z")
    assert not plan.redirect_allowed("https://example.org/a.7z")


@pytest.mark.parametrize(
    "field,value",
    [
        ("packageSize", -1),
        ("packageSize", True),
        ("packageSize", "12"),
        ("packageRealSize", -1),
        ("packageRealSize", None),
        ("packageUrls", []),
        ("packageUrls", "a"),
        ("packageUrls", [None]),
        ("packageUrls", ["https://evil.test/a.7z"]),
        ("packageUrls", ["http://download.bluepoch.com/a.7z"]),
        ("packageUrls", ["https://download.bluepoch.com.evil.test/a.7z"]),
        ("packageUrls", ["https://user@download.bluepoch.com/a.7z"]),
        ("packageUrls", ["https://download.bluepoch.com/../a.7z"]),
        ("packageUrls", ["https://download.bluepoch.com/%2e%2e/a.7z"]),
        ("packageUrls", ["/absolute.7z"]),
    ],
)
def test_bad_package(field, value):
    post = Post()
    post.resource["data"]["fullPackage"][field] = value
    with pytest.raises(dd.DirectError):
        r1999.resolve(post)


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"code": 500, "data": {}},
        {"code": 200, "data": []},
        {"code": 200, "data": {"latestVersion": 3}},
        {"code": 200, "data": {"latestVersion": "../3"}},
    ],
)
def test_bad_version(document):
    post = Post()
    post.version = document
    with pytest.raises(dd.DirectError):
        r1999.resolve(post)


@pytest.mark.parametrize(
    "document", [[], {}, {"code": 200, "data": {}}, {"code": 200, "data": {"fullPackage": []}}]
)
def test_bad_resource(document):
    with pytest.raises(dd.DirectError):
        r1999.resolve(Post(resource=document))


def test_bad_json():
    with pytest.raises(dd.DirectError):
        r1999.resolve(lambda *args: b"not JSON")


def test_resolve_at_latest_version_skips_resource():
    # The live reply for currentVersion == latestVersion is {..., "data": null}; Nadare must
    # not ask /resource at all, so nothing is planned and the version is the latest one.
    post = Post()
    plan = r1999.resolve(post, current_version="3.9.1")
    assert [url for url, _, _ in post.calls] == [
        "https://hotupdate-hw.sl916.com/diff-update/version"
    ]
    assert plan.version == "3.9.1"
    assert plan.archives == () and plan.files == ()


def test_null_resource_reply_means_up_to_date():
    # Saved live reply: {"code": 200, "msg": "success", "data": null}.
    resource = json.loads(SAME.read_bytes())
    assert resource["data"] is None
    post = Post(resource=resource)
    plan = r1999.resolve(post, current_version="3.9.0")
    assert plan.version == "3.9.1"
    assert plan.archives == () and plan.files == ()
    assert [url for url, _, _ in post.calls] == [
        "https://hotupdate-hw.sl916.com/diff-update/version",
        "https://hotupdate-hw.sl916.com/diff-update/resource",
    ]


def test_default_post_uses_shared_http(monkeypatch):
    import io

    from nadare.core import download

    seen = []

    def opened(url, **kwargs):
        seen.append((url, kwargs))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(download, "http_open", opened)
    assert r1999.default_post(r1999.API + "/version", b"{}", {"lang": "en"}) == b"{}"
    assert seen[0][1]["data"] == b"{}"
    assert seen[0][1]["headers"] == {"lang": "en"}
    guard = seen[0][1]["redirect_allowed"]
    assert guard(r1999.API + "/resource") and not guard("https://evil.test/x")


def test_backend_version_input_and_update(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.direct import R1999Backend
    from nadare.core import directkinds, profilenames

    backend = R1999Backend(
        make_context(tmp_path, "direct-r1999", {"prefix": str(tmp_path / "pfx")})
    )
    assert backend.plan_kwargs() == {"current_version": ""}
    backend._write_version_file("3.9.0", "r1999")
    assert backend.plan_kwargs() == {"current_version": "3.9.0"}
    post = Post()
    monkeypatch.setattr(r1999, "default_post", post)
    backend.check_update()
    assert len(post.calls) == 2 and post.calls[0][1]["currentVersion"] == "3.9.0"
    assert backend._read_latest()["version"] == "3.9.1"
    additions, _, _ = backend._direct_status_parts(None, False)
    assert additions["update_available"] is True
    backend._write_version_file("3.9.1", "r1999")
    additions, _, _ = backend._direct_status_parts(None, False)
    assert additions["update_available"] is False
    assert directkinds.tier_of(backend.kind) == "Experimental"
    assert profilenames.SHORT_NAMES[backend.kind] == "r1999"


def test_independent_archives_extract(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures/sevenz"
    blob = (fixture / "lzma2.7z").read_bytes()
    fetched = []

    def download(url, path, **kwargs):
        assert kwargs["redirect_allowed"](url)
        fetched.append(url)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(blob)

    monkeypatch.setattr(dd, "download", download)
    monkeypatch.setattr(dd, "remote_size", lambda *args, **kwargs: len(blob))
    post = Post(resource=json.loads(FIX.with_name("r1999_resource_full.json").read_bytes()))
    package = post.resource["data"]["fullPackage"]
    digest = hashlib.md5(blob).hexdigest()
    package["packageUrls"] = [
        re.sub(r"size_\d+_md5_[0-9a-f]{32}", f"size_{len(blob)}_md5_{digest}", url)
        for url in package["packageUrls"]
    ]
    package["packageSize"] = 20 * len(blob)
    plan = r1999.resolve(post)
    result = dd.sync(plan, tmp_path, workers=1)
    assert result["downloaded"] == 20 and len(fetched) == 20
    for path in (fixture / "expected").rglob("*"):
        if path.is_file():
            assert (
                tmp_path / path.relative_to(fixture / "expected")
            ).read_bytes() == path.read_bytes()
    assert len(list((tmp_path / dd.STATE_DIR).glob("*.done"))) == 20


@pytest.mark.parametrize("junk", [None, "1", "bad", "AB" * 16])
def test_package_md5_is_ignored(junk):
    resource = json.loads(FIX.with_name("r1999_diff.json").read_bytes())
    resource["data"]["diffPackage"]["packageMd5"] = junk
    plan = r1999.resolve(Post(resource=resource), current_version="3.9.0")
    assert plan.archives[0].source.size == 452086702
    assert plan.archives[0].source.md5 == "89f43d00e3631516823e34d18137aa6f"


@pytest.mark.parametrize(
    "fixture,current,count,total",
    [
        ("r1999_resource_full.json", "", 20, 29138096809),
        ("r1999_diff.json", "3.9.0", 1, 452086702),
        ("r1999_old.json", "3.8.2", 3, 4111022303),
    ],
)
def test_saved_reply_plan(fixture, current, count, total):
    resource = json.loads(FIX.with_name(fixture).read_bytes())
    # The saved full reply's packageSize (29 625 760 647) is not the parts' sum: accepted as is.
    plan = r1999.resolve(Post(resource=resource), current_version=current)
    assert len(plan.archives) == count
    assert sum(a.source.size for a in plan.archives) == total
    assert plan.extra["download_size"] == total
    package = resource["data"]["diffPackage"] or resource["data"]["fullPackage"]
    for archive, url in zip(plan.archives, package["packageUrls"], strict=True):
        assert archive.source.url == url
        assert archive.source.md5 == re.search(r"_md5_([0-9a-f]{32})_", url)[1]


@pytest.mark.parametrize("suffix", ["part.7z", "size_12_md5_bad_0.7z",
                                      "size_12_md5_" + "a" * 32 + "_0.zip"])
def test_missing_part_metadata(suffix):
    post = Post()
    post.resource["data"]["fullPackage"]["packageUrls"][0] = (
        "https://download.bluepoch.com/en/game/3.9.1/" + suffix
    )
    with pytest.raises(dd.DirectError):
        r1999.resolve(post)


def test_package_size_is_only_approximate():
    # The live full package says 29 625 760 647 B while its 20 parts sum to 29 138 096 809 B:
    # the parts' own sizes (also used for each md5 check) are what gets downloaded.
    post = Post()
    post.resource["data"]["fullPackage"]["packageSize"] += 487_663_838
    plan = r1999.resolve(post)
    assert plan.extra["download_size"] == sum(a.source.size for a in plan.archives)


@pytest.mark.parametrize("failure", ["network", "oversize"])
def test_default_post_errors(monkeypatch, failure):
    import io

    from nadare.core import download

    def opened(*args, **kwargs):
        if failure == "network":
            raise download.DownloadError("offline")
        return io.BytesIO(b"x" * (r1999._MAX_REPLY + 1))

    monkeypatch.setattr(download, "http_open", opened)
    with pytest.raises(dd.DirectError):
        r1999.default_post(r1999.API + "/version", b"{}", {})
