# SPDX-License-Identifier: GPL-3.0-or-later
"""Blue Protocol: Star Resonance (global, A Plus) direct source and lifecycle (#1783)."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
import urllib.response
import zipfile
from pathlib import Path
from typing import Any

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.backends import winegame as wg
from nadare.core import directdl as dd
from nadare.core import download

FIX = Path(__file__).parent / "fixtures" / "catalog"
BASE = "https://pc-download.playbpsr.com/apj_obt"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _zip_prefixed(files: dict[str, bytes], *, prefix: str = "game_mini/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if prefix:
            zf.writestr(prefix, b"")
        dirs: set[str] = set()
        for rel, body in files.items():
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = prefix + "/".join(parts[:i]) + "/"
                if d not in dirs:
                    dirs.add(d)
                    zf.writestr(d, b"")
            zf.writestr(prefix + rel, body)
    return buf.getvalue()


def _md5list_bytes(files: dict[str, bytes], *, with_patch_for: set[str] | None = None) -> bytes:
    patched = with_patch_for or set()
    rows: list[str] = []
    for rel, body in files.items():
        row = f"{_md5(body)},{len(body)},{rel}"
        if rel in patched:
            row += f",{_md5(b'patch-' + rel.encode())},123"
        rows.append(row)
    return ("\n".join(rows)).encode("utf-8")


def _verlist_bytes(version: str, md5list_raw: bytes) -> bytes:
    return f"{version},{_md5(md5list_raw)},{version},100,1".encode()


class _Fetch:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = dict(routes)
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Any = None) -> bytes:
        self.urls.append(url)
        if url not in self.routes:
            raise dd.DirectError(f"HTTP 404 for {url}")
        return self.routes[url]


def _fixture_routes(tree: str = "release_4.4") -> dict[str, bytes]:
    return {
        f"{BASE}/branch.txt": f"{tree}=release\n".encode(),
        f"{BASE}/{tree}/last/verlist.txt": (FIX / "bpsr_verlist.txt").read_bytes(),
        f"{BASE}/{tree}/last/md5list.txt": (FIX / "bpsr_md5list.txt").read_bytes(),
    }


# --------------------------------------------------------------- source parsing
def test_resolve_parses_feed_and_builds_expected_urls():
    from nadare.core.sources import bpsrglobal

    fetch = _Fetch(_fixture_routes())
    plan = bpsrglobal.resolve(fetch)
    assert fetch.urls == [
        f"{BASE}/branch.txt",
        f"{BASE}/release_4.4/last/verlist.txt",
        f"{BASE}/release_4.4/last/md5list.txt",
    ]
    assert plan.game_tag == "bpsr"
    assert plan.version == "1.0.46902.268"
    assert plan.exe == "BPSR.exe"
    assert plan.archives == ()
    assert len(plan.files) == 5
    assert plan.extra["tree"] == "release_4.4"
    assert plan.extra["total_bytes"] == 808496 + 11 + 909616 + 1564240 + 0
    assert plan.redirect_allowed is bpsrglobal.url_allowed

    by_path = {rf.path: rf for rf in plan.files}
    exe = by_path["BPSR.exe"]
    assert exe.url == f"{BASE}/release_4.4/1.0.46902.268/game_mini/BPSR.exe"
    assert exe.size == 808496
    assert exe.md5 == "8cdd3c3d3a876a8cb679902e3c2cf5a3"

    spaced = by_path["BPSR_Data/Resources/unity default resources"]
    assert spaced.url == (
        f"{BASE}/release_4.4/1.0.46902.268/game_mini/"
        "BPSR_Data/Resources/unity%20default%20resources"
    )
    assert by_path["BPSR_Data/il2cpp_data/Metadata/global-metadata.dat"].size == 0

    fresh = bpsrglobal.fresh_archive_plan(plan)
    assert fresh.files == ()
    assert fresh.version == plan.version
    assert fresh.redirect_allowed is bpsrglobal.url_allowed
    (arc,) = fresh.archives
    assert arc.source.url == f"{BASE}/release_4.4/1.0.46902.268/game_mini.zip"
    assert arc.unwrap == ("zip",)
    assert arc.strip_prefix == "game_mini/"
    assert arc.stream is False  # the redirect allow-list rules out streaming for now


def test_tree_name_comes_from_branch_txt_never_hardcoded():
    from nadare.core.sources import bpsrglobal

    routes = _fixture_routes(tree="release_5.1")
    routes[f"{BASE}/branch.txt"] = b"qa_5.1=qa\nrelease_5.1=release\n"
    fetch = _Fetch(routes)
    plan = bpsrglobal.resolve(fetch)
    assert plan.extra["tree"] == "release_5.1"
    assert plan.files[0].url.startswith(f"{BASE}/release_5.1/1.0.46902.268/game_mini/")
    fresh = bpsrglobal.fresh_archive_plan(plan)
    assert fresh.archives[0].source.url == f"{BASE}/release_5.1/1.0.46902.268/game_mini.zip"


def test_hdiff_entries_in_md5list_are_ignored():
    from nadare.core.sources import bpsrglobal

    md5list = (
        f"{_md5(b'MZ')},2,BPSR.exe,{_md5(b'p')},1\n{_md5(b'diff')},4,patches/BPSR.exe.hdiff\n"
    ).encode()
    routes = {
        f"{BASE}/branch.txt": b"release_4.4=release",
        f"{BASE}/release_4.4/last/verlist.txt": _verlist_bytes("1.0.0", md5list),
        f"{BASE}/release_4.4/last/md5list.txt": md5list,
    }
    plan = bpsrglobal.resolve(_Fetch(routes))
    assert [rf.path for rf in plan.files] == ["BPSR.exe"]


def test_parse_real_md5list_rows_keeps_three_and_five_column_rows():
    from nadare.core.sources import bpsrglobal

    raw = (FIX / "bpsr_md5list_real.txt").read_bytes()
    files, total = bpsrglobal.parse_md5list(
        raw, file_base=f"{BASE}/release_4.4/1.0.46902.268/game_mini"
    )
    by_path = {rf.path: rf for rf in files}
    assert len(files) == 4
    assert by_path["BPSR.exe"].size == 808496
    assert by_path["BPSR.exe"].md5 == "8cdd3c3d3a876a8cb679902e3c2cf5a3"
    assert by_path["BPSR.exe"].url.endswith("/game_mini/BPSR.exe")
    # A five-column row: the publisher's patch columns are ignored.
    assert by_path["BPSR_Data/boot.config"].size == 402
    spaced = by_path["BPSR_Data/Resources/unity default resources"]
    assert spaced.size == 1564240
    assert spaced.url.endswith(
        "/game_mini/BPSR_Data/Resources/unity%20default%20resources"
    )
    assert total == 808496 + 1001472 + 1564240 + 402


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{BASE}/branch.txt", True),
        ("https://PC-DOWNLOAD.PLAYBPSR.COM/apj_obt/x", True),
        ("https://pc-download.playbpsr.com:443/apj_obt/x", True),
        ("http://pc-download.playbpsr.com/apj_obt/x", False),
        ("https://pc-download.playbpsr.com:8443/apj_obt/x", False),
        ("https://exe.playbpsr.com/gclid/BPSR_Install.exe", False),
        ("https://evil.playbpsr.com/apj_obt/x", False),
        ("https://pc-download.playbpsr.com.evil.example/x", False),
        ("https://user@pc-download.playbpsr.com/x", False),
        ("http://127.0.0.1:8080/x", False),
        ("", False),
    ],
)
def test_url_allowed_restricts_to_https_playbpsr_download_host(url, expected):
    from nadare.core.sources import bpsrglobal

    assert bpsrglobal.url_allowed(url) is expected


@pytest.mark.parametrize(
    "branch_body",
    [
        b"",
        b"   \n",
        b"release_4.4",
        b"beta_4.4=beta",
        b"../evil=release",
        b"a/b=release",
        b"=release",
        b"a=release\nb=release",
        b"\xff\xfe=release",
    ],
)
def test_malformed_branch_is_refused(branch_body):
    from nadare.core.sources import bpsrglobal

    routes = _fixture_routes()
    routes[f"{BASE}/branch.txt"] = branch_body
    with pytest.raises(dd.DirectError):
        bpsrglobal.resolve(_Fetch(routes))


@pytest.mark.parametrize(
    "verlist_body",
    [
        b"",
        b"1.0.0",
        b",00000000000000000000000000000000",
        b"../1.0,00000000000000000000000000000000",
        b"not-a-version,00000000000000000000000000000000",
        b"1.0.0,not-an-md5",
        b"1.0.0,00000000000000000000000000000000",  # wrong md5 for md5list.txt
        b"\xff\xfe",
    ],
)
def test_malformed_or_mismatched_verlist_is_refused(verlist_body):
    from nadare.core.sources import bpsrglobal

    routes = _fixture_routes()
    routes[f"{BASE}/release_4.4/last/verlist.txt"] = verlist_body
    with pytest.raises(dd.DirectError):
        bpsrglobal.resolve(_Fetch(routes))


@pytest.mark.parametrize(
    "bad_row",
    [
        "not-an-md5,10,BPSR.exe",
        f"{'a' * 32},-1,BPSR.exe",
        f"{'a' * 32},abc,BPSR.exe",
        f"{'a' * 32},10,../outside.dll",
        f"{'a' * 32},10,/etc/passwd",
        f"{'a' * 32},10,BPSR_Data\\bad.bin",
        f"{'a' * 32},10,.nadare/evil.json",
        f"{'a' * 32},10,BPSR.exe,extra_col",
        f"{'a' * 32},10",
        f"{'a' * 32},10,BPSR.exe,a,b,c",
        f"{'a' * 32},10,",
    ],
)
def test_malformed_md5list_rows_are_refused(bad_row):
    from nadare.core.sources import bpsrglobal

    exe_row = f"{_md5(b'MZ')},2,BPSR.exe"
    raw = f"{exe_row}\n{bad_row}\n".encode()
    if bad_row.endswith("BPSR.exe"):
        raw = f"{bad_row}\n".encode()
    routes = {
        f"{BASE}/branch.txt": b"release_4.4=release",
        f"{BASE}/release_4.4/last/verlist.txt": _verlist_bytes("1.0.0", raw),
        f"{BASE}/release_4.4/last/md5list.txt": raw,
    }
    with pytest.raises(dd.DirectError):
        bpsrglobal.resolve(_Fetch(routes))


def test_md5list_without_exe_or_with_duplicate_path_is_refused():
    from nadare.core.sources import bpsrglobal

    no_exe = f"{_md5(b'x')},1,BPSR_Data/app.info\n".encode()
    routes = {
        f"{BASE}/branch.txt": b"release_4.4=release",
        f"{BASE}/release_4.4/last/verlist.txt": _verlist_bytes("1.0.0", no_exe),
        f"{BASE}/release_4.4/last/md5list.txt": no_exe,
    }
    with pytest.raises(dd.DirectError, match="BPSR.exe"):
        bpsrglobal.resolve(_Fetch(routes))

    dup = f"{_md5(b'a')},1,BPSR.exe\n{_md5(b'b')},1,bpsr.exe\n".encode()
    routes[f"{BASE}/release_4.4/last/verlist.txt"] = _verlist_bytes("1.0.0", dup)
    routes[f"{BASE}/release_4.4/last/md5list.txt"] = dup
    with pytest.raises(dd.DirectError, match="twice"):
        bpsrglobal.resolve(_Fetch(routes))


# -------------------------------------------------------- directdl strip_prefix
def test_directdl_strip_prefix_unwraps_without_prefix(tmp_path):
    blob = _zip_prefixed({"BPSR.exe": b"MZ", "BPSR_Data/app.info": b"info"}, prefix="game_mini/")
    zip_path = tmp_path / "game_mini.zip"
    zip_path.write_bytes(blob)
    dest = tmp_path / "dest"
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    arc = dd.Archive(
        "game_mini.zip",
        dd.RemoteFile("game_mini.zip", "https://example.invalid/game_mini.zip"),
        unwrap=("zip",),
        strip_prefix="game_mini/",
    )
    members = dd._unwrap(arc, zip_path, dest, dl_dir)
    assert sorted(members) == ["BPSR.exe", "BPSR_Data/app.info"]
    assert (dest / "BPSR.exe").read_bytes() == b"MZ"
    assert (dest / "BPSR_Data/app.info").read_bytes() == b"info"
    assert not (dest / "game_mini").exists()


@pytest.mark.parametrize(
    "member_name",
    [
        "outside.txt",
        "other_dir/BPSR.exe",
        "game_mini/../evil.exe",
        "game_mini/.nadare/evil.json",
        "Game_Mini/verlist.txt",
        "game_mini",
    ],
)
def test_directdl_strip_prefix_refuses_outside_or_unsafe_members(tmp_path, member_name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, b"bad")
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(buf.getvalue())
    dest = tmp_path / "dest"
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    arc = dd.Archive(
        "bad.zip",
        dd.RemoteFile("bad.zip", "https://example.invalid/bad.zip"),
        unwrap=("zip",),
        strip_prefix="game_mini/",
    )
    with pytest.raises(dd.DirectError):
        with zipfile.ZipFile(zip_path) as zf:
            dd._zip_entries(zf, "game_mini/")
    with pytest.raises(dd.DirectError):
        dd._unwrap(arc, zip_path, dest, dl_dir)
    assert not dest.exists() or not any(dest.iterdir())


def test_directdl_validate_plan_refuses_unsafe_strip_prefix():
    arc = dd.Archive(
        "bad.zip",
        dd.RemoteFile("bad.zip", "https://example.invalid/bad.zip"),
        unwrap=("zip",),
        strip_prefix="../escape/",
    )
    with pytest.raises(dd.DirectError):
        dd._validate_plan(dd.Plan("bpsr", "1", archives=(arc,)))


@pytest.mark.parametrize(
    "unwrap,members",
    [
        (("7z",), ()),
        (("slices",), (dd.Member("inside.bin", 0, 1),)),
    ],
)
def test_directdl_validate_plan_refuses_strip_prefix_outside_zip(unwrap, members):
    arc = dd.Archive(
        "bad.bin",
        dd.RemoteFile("bad.bin", "https://example.invalid/bad.bin"),
        unwrap=unwrap,
        members=members,
        strip_prefix="game_mini/",
    )
    with pytest.raises(dd.DirectError, match="strip_prefix"):
        dd._validate_plan(dd.Plan("bpsr", "1", archives=(arc,)))


# ---------------------------------------------- real downloader lifecycle tests
class _FakeTransport:
    """In-memory HTTPS/HTTP handler plugged into urllib beside download._SafeRedirectHandler.

    Only the network transport is faked; download.py's redirect guard, retries, size checks,
    part files, atomic renames, zip extraction and MD5 verification all run for real.
    """

    def __init__(self) -> None:
        self.routes: dict[str, bytes | tuple[int, str]] = {}
        self.hits: list[tuple[str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = self

        def _dispatch(req: urllib.request.Request) -> Any:
            method = req.get_method()
            url = req.full_url
            transport.hits.append((method, url))
            entry = transport.routes.get(url)
            if entry is None:
                raise urllib.error.HTTPError(
                    url, 404, "Not Found", email.message.Message(), io.BytesIO(b"")
                )
            if isinstance(entry, tuple):
                code, location = entry
                hdrs = email.message.Message()
                hdrs["Location"] = location
                hdrs["Content-Length"] = "0"
                resp = urllib.response.addinfourl(io.BytesIO(b""), hdrs, url, code=code)
                resp.msg = "Found"
                return resp
            body = entry
            start = 0
            code = 200
            hdrs = email.message.Message()
            range_hdr = req.get_header("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                span = range_hdr.removeprefix("bytes=")
                s_str, _, e_str = span.partition("-")
                start = int(s_str)
                end = int(e_str) if e_str else len(body) - 1
                chunk = body[start : end + 1]
                code = 206
                hdrs["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                hdrs["Content-Length"] = str(len(chunk))
                payload = b"" if method == "HEAD" else chunk
            else:
                hdrs["Content-Length"] = str(len(body))
                payload = b"" if method == "HEAD" else body
            resp = urllib.response.addinfourl(io.BytesIO(payload), hdrs, url, code=code)
            resp.msg = "OK"
            return resp

        class _FakeHTTP(urllib.request.HTTPHandler):
            def http_open(self, req: urllib.request.Request) -> Any:
                return _dispatch(req)

        class _FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req: urllib.request.Request) -> Any:
                return _dispatch(req)

        def build_opener(redirect_allowed=None):
            return urllib.request.build_opener(
                download._SafeRedirectHandler(redirect_allowed),
                _FakeHTTP(),
                _FakeHTTPS(),
            )

        monkeypatch.setattr(download, "_build_opener", build_opener)
        monkeypatch.setattr(download, "_pool_can_serve", lambda url: False)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _FakeTransport:
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(wg.WineGameBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(wg.WineGameBackend, "_ensure_direct_prefix", lambda self: None)
    t = _FakeTransport()
    t.install(monkeypatch)
    return t


def _publish(
    transport: _FakeTransport,
    tree: str,
    version: str,
    files: dict[str, bytes],
    *,
    zip_files: dict[str, bytes] | None = None,
    with_patch_for: set[str] | None = None,
) -> None:
    md5list = _md5list_bytes(files, with_patch_for=with_patch_for)
    verlist = _verlist_bytes(version, md5list)
    transport.routes[f"{BASE}/branch.txt"] = f"{tree}=release".encode()
    transport.routes[f"{BASE}/{tree}/last/verlist.txt"] = verlist
    transport.routes[f"{BASE}/{tree}/last/md5list.txt"] = md5list
    zip_blob = _zip_prefixed(files if zip_files is None else zip_files, prefix="game_mini/")
    transport.routes[f"{BASE}/{tree}/{version}/game_mini.zip"] = zip_blob
    for rel, body in files.items():
        quoted = urllib.request.quote(rel, safe="/")
        transport.routes[f"{BASE}/{tree}/{version}/game_mini/{quoted}"] = body


def _backend(tmp_path: Path):
    pfx = tmp_path / "pfx"
    (pfx / "drive_c").mkdir(parents=True, exist_ok=True)
    ctx = make_context(tmp_path, "direct-bpsr", {"prefix": str(pfx)}, "bpsr")
    return direct.BpsrBackend(ctx)


def test_full_lifecycle_fresh_update_verify_repair_remove_and_keep_user_files(
    transport: _FakeTransport, tmp_path: Path
):
    tree = "release_4.4"
    v1 = {
        "BPSR.exe": b"MZ-v1",
        "BPSR_Data/app.info": b"APlus\nBPSR",
        "BPSR_Data/Resources/unity default resources": b"res-v1",
        "BPSR_Data/old.pak": b"old-content",
    }
    _publish(transport, tree, "1.0.0", v1, with_patch_for={"BPSR.exe"})
    b = _backend(tmp_path)

    # 1. Fresh install: downloads game_mini.zip, unwraps without game_mini/, no per-file GETs.
    b.install()
    root = b.install_dir()
    assert not (root / "game_mini").exists()
    for rel, body in v1.items():
        assert (root / rel).read_bytes() == body
    assert b.status().state == "installed"
    assert b.status().version == "1.0.0"
    fetched_v1 = [u for m, u in transport.hits if m == "GET"]
    assert f"{BASE}/{tree}/1.0.0/game_mini.zip" in fetched_v1
    assert not any(f"{BASE}/{tree}/1.0.0/game_mini/" in u for u in fetched_v1)
    assert b.verify()["verified"] is True

    # Plant user files that no md5list ever named.
    user_root = root / "my-screenshot.png"
    user_sub = root / "BPSR_Data" / "user.cfg"
    user_root.write_bytes(b"user-png")
    user_sub.write_bytes(b"user-cfg")

    # 2. Update: changed + added files fetched per-path; unchanged skipped; zip NOT fetched.
    v2 = {
        "BPSR.exe": b"MZ-v2",  # changed
        "BPSR_Data/app.info": b"APlus\nBPSR",  # unchanged
        "BPSR_Data/Resources/unity default resources": b"res-v1",  # unchanged
        "BPSR_Data/old.pak": b"old-content",  # unchanged
        "BPSR_Data/new.pak": b"brand-new",  # added
    }
    _publish(transport, tree, "1.1.0", v2, with_patch_for={"BPSR.exe"})
    transport.hits.clear()
    b.update()
    fetched_v2 = [u for m, u in transport.hits if m == "GET"]
    assert f"{BASE}/{tree}/1.1.0/game_mini.zip" not in fetched_v2
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR.exe" in fetched_v2
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/new.pak" in fetched_v2
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/app.info" not in fetched_v2
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/old.pak" not in fetched_v2
    assert not any(".hdiff" in u for u in fetched_v2)
    assert b.status().version == "1.1.0"
    for rel, body in v2.items():
        assert (root / rel).read_bytes() == body

    # 3. Verify after update: healthy, no full re-download planned.
    verify_ok = b.verify()
    assert verify_ok["verified"] is True
    assert b._missing_entries() == []

    # 4. Repair: corrupt one file (same size, bad md5) and delete another; repair fetches only them.
    (root / "BPSR_Data/new.pak").write_bytes(b"CORRUPTED")  # same length (9 B), wrong md5
    (root / "BPSR_Data/app.info").unlink()
    verify_bad = b.verify()
    assert verify_bad["verified"] is False
    assert "BPSR_Data/new.pak" in verify_bad["message"]
    assert "BPSR_Data/app.info" in verify_bad["message"]

    transport.hits.clear()
    b.repair()
    repaired = [u for m, u in transport.hits if m == "GET"]
    assert f"{BASE}/{tree}/1.1.0/game_mini.zip" not in repaired
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/new.pak" in repaired
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/app.info" in repaired
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR.exe" not in repaired
    assert (root / "BPSR_Data/new.pak").read_bytes() == b"brand-new"
    assert (root / "BPSR_Data/app.info").read_bytes() == b"APlus\nBPSR"
    assert b.verify()["verified"] is True

    # Repair when BPSR.exe itself was deleted still repairs only BPSR.exe (no full zip).
    (root / "BPSR.exe").unlink()
    transport.hits.clear()
    b.repair()
    repaired_exe = [u for m, u in transport.hits if m == "GET"]
    assert f"{BASE}/{tree}/1.1.0/game_mini.zip" not in repaired_exe
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR.exe" in repaired_exe
    assert (root / "BPSR.exe").read_bytes() == b"MZ-v2"

    # 5. Update with a removed file: old.pak deleted, user files untouched, verify healthy.
    v3 = {k: v for k, v in v2.items() if k != "BPSR_Data/old.pak"}
    _publish(transport, tree, "1.2.0", v3)
    transport.hits.clear()
    b.update()
    assert not (root / "BPSR_Data/old.pak").exists()
    assert user_root.read_bytes() == b"user-png"
    assert user_sub.read_bytes() == b"user-cfg"
    assert b.status().version == "1.2.0"
    assert b.verify()["verified"] is True


def test_fresh_install_fetches_any_delta_missing_from_game_mini_zip(
    transport: _FakeTransport, tmp_path: Path
):
    tree = "release_4.4"
    full = {"BPSR.exe": b"MZ", "BPSR_Data/hotfix.bin": b"hotfix"}
    partial_zip = {"BPSR.exe": b"MZ"}
    _publish(transport, tree, "1.0.0", full, zip_files=partial_zip)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    assert (root / "BPSR.exe").read_bytes() == b"MZ"
    assert (root / "BPSR_Data/hotfix.bin").read_bytes() == b"hotfix"
    fetched = [u for m, u in transport.hits if m == "GET"]
    assert f"{BASE}/{tree}/1.0.0/game_mini.zip" in fetched
    assert f"{BASE}/{tree}/1.0.0/game_mini/BPSR_Data/hotfix.bin" in fetched
    assert f"{BASE}/{tree}/1.0.0/game_mini/BPSR.exe" not in fetched
    assert b.verify()["verified"] is True


def test_removed_paths_never_escape_install_dir_or_follow_symlinks(
    transport: _FakeTransport, tmp_path: Path
):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/old.pak": b"old", "BPSR_Data/link.pak": b"orig"}
    v2 = {"BPSR.exe": b"MZ"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()

    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "victim.bin").write_text("keep")
    (root / "linked").symlink_to(elsewhere)
    # Replace a previously managed file with a symlink pointing outside.
    (root / "BPSR_Data/link.pak").unlink()
    (root / "BPSR_Data/link.pak").symlink_to(outside)

    record_path = root / dd.STATE_DIR / "manifest-files.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["paths"] += ["../../../../outside.txt", str(outside), "linked/victim.bin"]
    record_path.write_text(json.dumps(record), encoding="utf-8")

    _publish(transport, tree, "1.1.0", v2)
    b.update()
    assert not (root / "BPSR_Data/old.pak").exists()
    assert outside.read_text() == "keep"
    assert (elsewhere / "victim.bin").read_text() == "keep"
    assert (root / "BPSR_Data/link.pak").is_symlink()


def test_failed_update_deletes_nothing_and_keeps_previous_inventory(
    transport: _FakeTransport, tmp_path: Path
):
    from nadare.core.backends import BackendError

    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/old.pak": b"old"}
    v2 = {"BPSR.exe": b"MZ", "BPSR_Data/missing.pak": b"missing"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    record_path = root / dd.STATE_DIR / "manifest-files.json"
    before = record_path.read_bytes()

    _publish(transport, tree, "1.1.0", v2)
    del transport.routes[f"{BASE}/{tree}/1.1.0/game_mini/BPSR_Data/missing.pak"]
    with pytest.raises(BackendError):
        b.update()
    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"
    assert record_path.read_bytes() == before


def test_another_games_manifest_inventory_is_ignored(transport: _FakeTransport, tmp_path: Path):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/old.pak": b"old"}
    v2 = {"BPSR.exe": b"MZ"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    record_path = root / dd.STATE_DIR / "manifest-files.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["game"] = "other-game"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    _publish(transport, tree, "1.1.0", v2)
    b.update()
    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_unremovable_dropped_file_is_retried_by_repair(transport: _FakeTransport, tmp_path: Path):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/sub/gone.pak": b"gone"}
    v2 = {"BPSR.exe": b"MZ"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    sub = root / "BPSR_Data/sub"
    sub.chmod(0o500)
    try:
        _publish(transport, tree, "1.1.0", v2)
        b.update()
        assert (sub / "gone.pak").exists()
        assert b.status().data.get("leftover_files") == 1
    finally:
        sub.chmod(0o700)
    b.repair()
    assert not (sub / "gone.pak").exists()
    assert "leftover_files" not in b.status().data


# ---------------------------------------------------------- redirect host guard
def test_feed_redirect_off_playbpsr_host_is_refused(transport: _FakeTransport):
    from nadare.core.sources import bpsrglobal

    transport.routes[f"{BASE}/branch.txt"] = (302, "https://evil.example/branch.txt")
    transport.routes["https://evil.example/branch.txt"] = b"release_4.4=release"
    with pytest.raises(dd.DirectError, match="disallowed"):
        bpsrglobal.resolve()
    assert not any("evil.example" in u for _, u in transport.hits)


def test_download_redirect_off_host_is_refused_and_same_host_redirect_works(
    transport: _FakeTransport, tmp_path: Path
):
    from nadare.core.backends import BackendError

    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ-v1"}
    _publish(transport, tree, "1.0.0", v1)
    zip_url = f"{BASE}/{tree}/1.0.0/game_mini.zip"
    real_zip = transport.routes[zip_url]

    # Off-host redirect on game_mini.zip is refused.
    transport.routes[zip_url] = (302, "https://evil.example/game_mini.zip")
    transport.routes["https://evil.example/game_mini.zip"] = real_zip
    b = _backend(tmp_path)
    with pytest.raises(BackendError):
        b.install()
    assert not (b.install_dir() / "BPSR.exe").exists()
    assert not any("evil.example" in u for _, u in transport.hits)

    # Same-host HTTPS redirect on game_mini.zip is allowed.
    alt_zip_url = f"{BASE}/cdn2/{tree}/1.0.0/game_mini.zip"
    transport.routes[zip_url] = (302, alt_zip_url)
    transport.routes[alt_zip_url] = real_zip
    b.install()
    assert (b.install_dir() / "BPSR.exe").read_bytes() == b"MZ-v1"

    # Off-host redirect on a per-file update is refused.
    v2 = {"BPSR.exe": b"MZ-v2"}
    _publish(transport, tree, "1.1.0", v2)
    exe_url = f"{BASE}/{tree}/1.1.0/game_mini/BPSR.exe"
    transport.routes[exe_url] = (302, "https://evil.example/BPSR.exe")
    transport.routes["https://evil.example/BPSR.exe"] = b"MZ-v2"
    transport.hits.clear()
    with pytest.raises(BackendError):
        b.update()
    assert (b.install_dir() / "BPSR.exe").read_bytes() == b"MZ-v1"
    assert not any("evil.example" in u for _, u in transport.hits)


# ------------------------------------------------ registration & catalogue info
def test_backend_is_registered_experimental_with_steam_and_ace_notes():
    from nadare.backends.direct import BpsrBackend
    from nadare.core import artindex, directkinds, profilenames

    assert "direct-bpsr" in directkinds.DIRECT_KINDS
    assert directkinds.tier_of("direct-bpsr") == directkinds.TIER_EXPERIMENTAL
    assert directkinds.ART["direct-bpsr"] == 3681810
    assert artindex.ALLOWED_HOSTS["direct-bpsr"] == artindex.STEAM_HOSTS
    assert profilenames.SHORT_NAMES["direct-bpsr"] == "bpsr"
    assert BpsrBackend.kind == "direct-bpsr"
    assert BpsrBackend.direct_exe == "BPSR.exe"
    assert BpsrBackend.plan_factory is not None
    # The finished install (md5list.txt) and the peak while the zip is also on disk.
    assert BpsrBackend.install_size_hint == 47_980_628_350
    assert BpsrBackend.install_space_hint == 42_772_592_920 + 47_980_628_350
    notice, link, _label = BpsrBackend.page_notice
    assert "3681810" in notice and "Steam" in notice
    assert "Anti-Cheat Expert" in notice and "Wine" in notice
    assert "3681810" in link
    from nadare.core.backends import page_summary

    summary = page_summary("direct-bpsr")
    assert summary.startswith("Global (A Plus) build")
    assert len(summary) <= 90


def test_sizes_use_one_unit_and_the_page_store_and_progress_agree():
    """The page's download/installed/free numbers match the Store card and the bar's total."""
    import re

    from nadare.backends.direct import BpsrBackend
    from nadare.core import storecatalog
    from nadare.core.backends import install_space_hint
    from nadare.core.diskspace import human_size

    download = BpsrBackend.download_size_hint
    installed = BpsrBackend.install_size_hint
    peak = BpsrBackend.install_space_hint
    assert download == 42_772_592_920  # game_mini.zip, what the progress bar totals
    assert installed == 47_980_628_350  # md5list.txt's 9,719 rows, what the Store shows
    assert peak == download + installed  # both on disk while the zip unpacks

    notice = BpsrBackend.page_notice[0]
    assert f"about {human_size(download)} to download" in notice
    assert f"{human_size(installed)} installed" in notice
    from nadare.core.diskspace import free_needed

    assert f"keep about {human_size(free_needed(peak))} free" in notice
    assert not re.search(r"\d\s*GB\b", notice), "no decimal GB next to the app's GiB"

    row = next(entry for entry in storecatalog.entries() if entry.kind == "direct-bpsr")
    # The card shows what a fresh install fetches: the same figure the sheet names.
    assert row.size_bytes == download
    assert f"about {human_size(download)} to download" in notice
    assert f"{human_size(installed)} installed" in notice
    assert install_space_hint("direct-bpsr") == peak


def test_the_progress_formatter_and_the_page_notice_agree_on_the_download():
    """The bar (download._emit_progress, which directdl also uses) and the page agree."""
    from nadare.backends.direct import BpsrBackend
    from nadare.core import download
    from nadare.core.diskspace import human_size

    events: list = []
    download._emit_progress(events.append, "download", 0, BpsrBackend.download_size_hint)
    shown = human_size(BpsrBackend.download_size_hint)
    assert events and f"/ {shown}" in events[0].message
    assert f"about {shown} to download" in BpsrBackend.page_notice[0]


def _real_bpsr(tmp_path):
    from nadare.backends.direct import BpsrBackend

    return BpsrBackend(
        make_context(
            tmp_path,
            "direct-bpsr",
            {"install_dir": str(tmp_path / "install"), "prefix": str(tmp_path / "prefix")},
        )
    )


def test_a_fresh_install_is_checked_against_the_peak_not_the_finished_size(tmp_path, monkeypatch):
    """The zip and the tree it unpacks to are both on disk, so reserve both."""
    from nadare.backends.direct import BpsrBackend
    from nadare.core import diskspace

    backend = _real_bpsr(tmp_path)
    checked = []
    monkeypatch.setattr(
        diskspace, "check_space", lambda path, needed, margin=0.0: checked.append(int(needed))
    )
    size, _path = diskspace.install_space(backend)
    assert size == BpsrBackend.install_space_hint == 90_753_221_270
    assert checked == [90_753_221_270]
    assert size != BpsrBackend.install_size_hint


def test_the_install_promise_reserves_the_peak_not_the_finished_size(tmp_path, monkeypatch):
    from nadare.backends.direct import BpsrBackend
    from nadare.core import diskspace, operation_policy

    backend = _real_bpsr(tmp_path)
    promised = {}

    def reserve(path, needed_bytes, *, owner, margin=None):
        promised["needed"] = int(needed_bytes)
        return "reservation"

    monkeypatch.setattr(diskspace, "tree_bytes", lambda path, **kwargs: 0)
    monkeypatch.setattr(diskspace, "reserve", reserve)
    assert operation_policy.reserve_install_space(backend, "bpsr") == "reservation"
    assert promised["needed"] == BpsrBackend.install_space_hint
    assert promised["needed"] != BpsrBackend.install_size_hint


def test_the_peak_falls_back_to_the_installed_size_when_it_is_not_declared(
    tmp_path, monkeypatch
):
    from nadare.backends.direct import BpsrBackend
    from nadare.core import diskspace, operation_policy

    monkeypatch.setattr(BpsrBackend, "install_space_hint", None)
    backend = _real_bpsr(tmp_path)
    checked = []
    monkeypatch.setattr(
        diskspace, "check_space", lambda path, needed, margin=0.0: checked.append(int(needed))
    )
    size, _path = diskspace.install_space(backend)
    assert size == BpsrBackend.install_size_hint
    assert checked == [BpsrBackend.install_size_hint]

    promised = {}
    monkeypatch.setattr(diskspace, "tree_bytes", lambda path, **kwargs: 0)
    monkeypatch.setattr(
        diskspace, "reserve", lambda path, needed, *, owner: promised.setdefault("n", int(needed))
    )
    operation_policy.reserve_install_space(backend, "bpsr")
    assert promised["n"] == BpsrBackend.install_size_hint


def test_streamed_zip_install_strips_the_prefix_without_storing_the_archive(tmp_path):
    import io
    import zipfile

    from _httpserver import Server

    from nadare.core import directdl

    files = {"game_mini/BPSR.exe": b"exe" * 4000, "game_mini/BPSR_Data/app.info": b"x" * 11}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("game_mini/", b"")
        for name, data in files.items():
            z.writestr(name, data)
    blob = buf.getvalue()
    server = Server({"/game_mini.zip": blob})
    try:
        archive = directdl.Archive(
            "game_mini.zip",
            directdl.RemoteFile("game_mini.zip", server.url + "/game_mini.zip", len(blob)),
            unwrap=("zip",),
            stream=True,
            strip_prefix="game_mini/",
        )
        directdl.sync(directdl.Plan("BPSR", "1", archives=(archive,)), tmp_path)
        assert server.ranges  # really streamed
    finally:
        server.close()
    assert (tmp_path / "BPSR.exe").read_bytes() == files["game_mini/BPSR.exe"]
    assert (tmp_path / "BPSR_Data/app.info").read_bytes() == b"x" * 11
    assert not (tmp_path / "game_mini").exists()
    assert not (tmp_path / directdl.DL_DIR).exists()


# ------------------------------------- fix round 1: interruption, links, checks
def test_interrupted_fresh_install_with_exe_and_kept_zip_resumes_the_zip(
    transport: _FakeTransport, tmp_path: Path
):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ-v1", "BPSR_Data/app.info": b"APlus"}
    _publish(transport, tree, "1.0.0", v1)
    zip_url = f"{BASE}/{tree}/1.0.0/game_mini.zip"
    b = _backend(tmp_path)
    root = b.install_dir()
    root.mkdir(parents=True)
    # A killed fresh install: the exe was merged (it is early in the real zip), no version
    # record and no inventory were written, the marker stayed and the archive was kept.
    (root / "BPSR.exe").write_bytes(b"MZ-v1")
    (root / dd.STATE_DIR).mkdir()
    (root / ".nadare-install-incomplete").write_text("sync in progress\n", encoding="utf-8")
    dl = root / dd.DL_DIR
    dl.mkdir()
    (dl / "game_mini.zip").write_bytes(transport.routes[zip_url])

    transport.hits.clear()
    b.install()

    fetched = [u for m, u in transport.hits if m == "GET"]
    assert zip_url in fetched
    assert not any(u.startswith(f"{BASE}/{tree}/1.0.0/game_mini/") for u in fetched)
    assert b.status().version == "1.0.0"
    for rel, body in v1.items():
        assert (root / rel).read_bytes() == body
    assert b.verify()["verified"] is True


def test_a_stale_game_mini_zip_is_removed_by_a_per_file_update(
    transport: _FakeTransport, tmp_path: Path
):
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ-v1", "BPSR_Data/app.info": b"APlus"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    dl = root / dd.DL_DIR
    dl.mkdir(exist_ok=True)
    (dl / "game_mini.zip").write_bytes(b"stranded archive")

    v2 = {"BPSR.exe": b"MZ-v2", "BPSR_Data/app.info": b"APlus"}
    _publish(transport, tree, "1.1.0", v2)
    transport.hits.clear()
    b.update()

    assert not (dl / "game_mini.zip").exists()
    fetched = [u for m, u in transport.hits if m == "GET"]
    assert not any(u.endswith("/game_mini.zip") for u in fetched)
    assert f"{BASE}/{tree}/1.1.0/game_mini/BPSR.exe" in fetched
    assert (root / "BPSR.exe").read_bytes() == b"MZ-v2"
    assert b.verify()["verified"] is True


def test_linked_state_folder_is_refused_by_install_update_and_repair(
    transport: _FakeTransport, tmp_path: Path
):
    from nadare.core.backends import BackendError

    tree = "release_4.4"
    _publish(transport, tree, "1.0.0", {"BPSR.exe": b"MZ"})
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    state = root / dd.STATE_DIR
    outside = tmp_path / "outside-state"
    shutil.move(str(state), str(outside))
    state.symlink_to(outside, target_is_directory=True)
    manifest = outside / "manifest-files.json"
    before = manifest.read_bytes()

    transport.hits.clear()
    for action in (b.update, b.repair, b.install):
        with pytest.raises(BackendError, match="symlink"):
            action()
        assert manifest.read_bytes() == before
        assert state.is_symlink()
    assert transport.hits == []


def test_is_fresh_install_does_not_count_a_linked_inventory(tmp_path: Path):
    b = _backend(tmp_path)
    root = b.install_dir()
    state = root / dd.STATE_DIR
    state.mkdir(parents=True)
    outside = tmp_path / "outside-inventory.json"
    outside.write_text(
        json.dumps({"game": "bpsr", "version": "1.0.0", "paths": ["BPSR.exe"]}),
        encoding="utf-8",
    )
    (state / "manifest-files.json").symlink_to(outside)

    assert b._is_fresh_install() is True


def _install_then_replace_inventory(
    transport: _FakeTransport, tmp_path: Path
) -> tuple[Any, Path, Path]:
    """Install v1 with an old.pak, then let the test replace its inventory record."""
    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ", "BPSR_Data/old.pak": b"old"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    record = root / dd.STATE_DIR / "manifest-files.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.unlink(missing_ok=True)
    _publish(transport, tree, "1.1.0", {"BPSR.exe": b"MZ"})
    return b, root, record


def test_inventory_reached_through_a_symlink_is_treated_as_absent(
    transport: _FakeTransport, tmp_path: Path
):
    b, root, record = _install_then_replace_inventory(transport, tmp_path)
    outside = tmp_path / "outside-inventory.json"
    outside.write_text(
        json.dumps({"game": "bpsr", "version": "1.0.0", "paths": ["BPSR_Data/old.pak"]}),
        encoding="utf-8",
    )
    before = outside.read_bytes()
    record.symlink_to(outside)

    b.update()

    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"
    assert outside.read_bytes() == before


def test_inventory_that_is_not_a_regular_file_is_treated_as_absent(
    transport: _FakeTransport, tmp_path: Path
):
    b, root, record = _install_then_replace_inventory(transport, tmp_path)
    payload = json.dumps(
        {"game": "bpsr", "version": "1.0.0", "paths": ["BPSR_Data/old.pak"]}
    ).encode()
    os.mkfifo(record)

    def write_payload() -> None:
        try:
            with open(record, "wb") as handle:
                handle.write(payload)
        except OSError:
            pass

    writer = threading.Thread(target=write_payload, daemon=True)
    writer.start()
    b.update()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"


def test_oversize_inventory_is_treated_as_absent(transport: _FakeTransport, tmp_path: Path):
    from nadare.backends import direct_bpsr

    b, root, record = _install_then_replace_inventory(transport, tmp_path)
    # >16 MB, and its first 16 MB + 1 bytes are still valid JSON (the rest is whitespace):
    # only the size limit can refuse it.
    body = json.dumps({"game": "bpsr", "version": "1.0.0", "paths": ["BPSR_Data/old.pak"]})
    record.write_text(body + "\n" * direct_bpsr._MANIFEST_FILES_LIMIT, encoding="utf-8")
    assert record.stat().st_size > direct_bpsr._MANIFEST_FILES_LIMIT

    b.update()

    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"
    assert record.stat().st_size < direct_bpsr._MANIFEST_FILES_LIMIT


def test_bad_json_inventory_is_treated_as_absent(transport: _FakeTransport, tmp_path: Path):
    b, root, record = _install_then_replace_inventory(transport, tmp_path)
    record.write_bytes(b"{not json")

    b.update()

    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"


def test_inventory_with_an_unsafe_path_is_treated_as_absent(
    transport: _FakeTransport, tmp_path: Path
):
    b, root, record = _install_then_replace_inventory(transport, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    record.write_text(
        json.dumps({"game": "bpsr", "version": "1.0.0", "paths": ["../../outside.txt"]}),
        encoding="utf-8",
    )

    b.update()

    assert (root / "BPSR_Data/old.pak").read_bytes() == b"old"
    assert outside.read_text(encoding="utf-8") == "keep"


def test_check_update_after_an_install_does_not_hash_or_plan_files(
    transport: _FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tree = "release_4.4"
    _publish(transport, tree, "1.0.0", {"BPSR.exe": b"MZ", "BPSR_Data/app.info": b"APlus"})
    b = _backend(tmp_path)
    b.install()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("check_update hashed or planned the installed files")

    monkeypatch.setattr(dd, "_digest_pass", boom)
    monkeypatch.setattr(dd, "plan_missing", boom)
    transport.hits.clear()
    b.check_update()

    assert transport.hits == []


def test_fresh_zip_install_checks_sizes_only_on_the_follow_up_pass(
    transport: _FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tree = "release_4.4"
    v1 = {
        "BPSR.exe": b"MZ-v1",
        "BPSR_Data/app.info": b"APlus",
        "BPSR_Data/Resources/unity default resources": b"res",
    }
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    real_verify = dd._verify_remote
    checks: list[tuple[str, bool]] = []

    def spy(path: Path, rf: Any, *, hashes: bool = True, cancel: Any = None) -> bool:
        checks.append((rf.path, hashes))
        return real_verify(path, rf, hashes=hashes, cancel=cancel)

    def no_hashing(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the fresh zip install hashed the tree again")

    with monkeypatch.context() as patch:
        patch.setattr(dd, "_verify_remote", spy)
        patch.setattr(dd, "_digest_pass", no_hashing)
        b.install()

    assert {path for path, _ in checks} == set(v1)
    assert checks and not any(hashes for _, hashes in checks)
    assert b.status().version == "1.0.0"
    assert b.verify()["verified"] is True


def test_resumed_fresh_install_reuses_the_zip_record_without_hashing(
    transport: _FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from nadare.core.gameversion import VERSION_FILE

    tree = "release_4.4"
    v1 = {"BPSR.exe": b"MZ-v1", "BPSR_Data/app.info": b"APlus"}
    _publish(transport, tree, "1.0.0", v1)
    b = _backend(tmp_path)
    b.install()
    root = b.install_dir()
    # A kill after the zip's own .done record, before the version and inventory records.
    (root / VERSION_FILE).unlink()
    (root / dd.STATE_DIR / "manifest-files.json").unlink()
    transport.hits.clear()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the resumed install hashed the extracted tree again")

    with monkeypatch.context() as patch:
        patch.setattr(dd, "_crc32_file", boom)
        patch.setattr(dd, "_digest_pass", boom)
        b.install()

    fetched = [u for m, u in transport.hits if m == "GET"]
    assert not any(u.endswith("/game_mini.zip") for u in fetched)
    assert b.status().version == "1.0.0"
    assert b.verify()["verified"] is True
