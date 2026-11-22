# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 2: a Yostar manifest becomes a direct-download plan.

``yostar.to_plan`` is the bridge that lets the Yostar kinds install through
DirectBackend. These tests pin that the plan says the same thing the manifest
does (one file each, same URL, size and CRC-64), that it decides what is missing
exactly as ``yostar.plan`` does on the same disk, and that it keeps the manifest's
host rules: no untrusted host, no plain http, no loopback base from a remote manifest.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.parse
from pathlib import Path

import pytest

from nadare.core import directdl, yostar


def _entry(path: str, data: bytes) -> yostar.ManifestFile:
    return yostar.ManifestFile(path, len(data), yostar.crc64(data))


def _manifest(
    files: dict[str, bytes],
    base_url: str = "https://cdn.yo-star.com/game/1.2",
    *,
    source: str = "",
) -> yostar.Manifest:
    return yostar.Manifest(
        game_tag="Arknights_EN",
        version="1.2.3",
        base_url=base_url,
        generated_at="",
        files=tuple(_entry(path, data) for path, data in files.items()),
        source=source,
    )


FILES = {
    "Arknights.exe": b"exe-bytes",
    "data/a.bin": b"a" * 100,
    "data/b.bin": b"b" * 50,
    "data/c d#e.bin": b"spaces and hash",
    "data/empty.bin": b"",
    "data/gone.bin": b"never written",
}


def test_one_remote_file_per_manifest_entry() -> None:
    manifest = _manifest(FILES)
    result = yostar.to_plan(manifest, exe="Arknights.exe")
    assert isinstance(result, directdl.Plan)
    assert result.game_tag == "Arknights_EN"
    assert result.version == "1.2.3"
    assert result.exe == "Arknights.exe"
    assert result.archives == ()
    assert result.sophon == ()
    assert len(result.files) == len(manifest.files)
    for rf, entry in zip(result.files, manifest.files, strict=True):
        assert rf.path == entry.path
        assert rf.url == yostar.file_url(manifest, entry)
        assert rf.size == entry.size
        assert rf.crc64 == entry.crc64
        # Yostar declares nothing else: no other hash may sneak in and be checked.
        assert rf.md5 is None and rf.sha256 is None and rf.crc32 is None
        assert rf.headers == ()


def test_urls_are_quoted_under_the_base() -> None:
    result = yostar.to_plan(_manifest(FILES, "https://cdn.yo-star.com/game/1.2/"))
    urls = {rf.path: rf.url for rf in result.files}
    assert urls["data/c d#e.bin"] == "https://cdn.yo-star.com/game/1.2/data/c%20d%23e.bin"
    assert urls["Arknights.exe"] == "https://cdn.yo-star.com/game/1.2/Arknights.exe"


def test_empty_file_keeps_crc_zero_not_none() -> None:
    result = yostar.to_plan(_manifest({"empty.bin": b""}))
    assert result.files[0].crc64 == 0
    assert result.files[0].size == 0


def test_plan_passes_directdl_validation() -> None:
    directdl._validate_plan(yostar.to_plan(_manifest(FILES)))


def _lay_out_disk(root: Path) -> None:
    """Every case ``yostar.plan`` distinguishes, on one disk."""
    (root / "data").mkdir(parents=True)
    (root / "Arknights.exe").write_bytes(FILES["Arknights.exe"])  # current
    (root / "data/a.bin").write_bytes(b"x" * 100)  # same size, wrong CRC
    (root / "data/b.bin").write_bytes(b"b" * 49)  # wrong size
    (root / "data/c d#e.bin").mkdir()  # a folder where a file belongs
    (root / "data/empty.bin").write_bytes(b"")  # current, empty
    # data/gone.bin: absent


@pytest.mark.parametrize("verify", [True, False])
def test_missing_files_match_yostar_plan(tmp_path: Path, verify: bool) -> None:
    _lay_out_disk(tmp_path)
    manifest = _manifest(FILES)
    expected = [entry.path for entry in yostar.plan(manifest, tmp_path, verify=verify)]
    files, archives = directdl.plan_missing(
        yostar.to_plan(manifest), tmp_path, verify=verify
    )
    assert [rf.path for rf in files] == expected
    assert archives == []
    # The disk really does cover the cases: the parity is not vacuous.
    if verify:
        assert expected == ["data/a.bin", "data/b.bin", "data/c d#e.bin", "data/gone.bin"]
    else:
        assert expected == ["data/b.bin", "data/c d#e.bin", "data/gone.bin"]


def test_all_current_means_nothing_missing(tmp_path: Path) -> None:
    for path, data in FILES.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = _manifest(FILES)
    assert yostar.plan(manifest, tmp_path) == []
    assert directdl.plan_missing(yostar.to_plan(manifest), tmp_path) == ([], [])


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example.com/game",  # not a Yostar host
        "https://yo-star.com.evil.example/game",  # suffix trick
        "http://cdn.yo-star.com/game",  # plain http
        "https://user:pw@cdn.yo-star.com/game",  # credentials
        "",
    ],
)
def test_untrusted_base_is_refused(base_url: str) -> None:
    with pytest.raises(yostar.ManifestRefused):
        yostar.to_plan(_manifest(FILES, base_url))


def test_allowed_hosts_widen_like_sync() -> None:
    manifest = _manifest(FILES, "https://mirror.example.net/game")
    with pytest.raises(yostar.ManifestRefused):
        yostar.to_plan(manifest)
    result = yostar.to_plan(manifest, allowed_hosts={"mirror.example.net"})
    assert result.files[0].url.startswith("https://mirror.example.net/game/")


def test_loopback_base_only_from_a_local_manifest() -> None:
    local = _manifest(FILES, "http://127.0.0.1:9/game", source="/tmp/manifest.json")
    assert yostar.to_plan(local).files
    remote = _manifest(
        FILES, "http://127.0.0.1:9/game", source="https://cdn.yo-star.com/manifest.json"
    )
    with pytest.raises(yostar.ManifestRefused, match="loopback"):
        yostar.to_plan(remote)


def test_sync_and_to_plan_share_one_trust_check(tmp_path: Path) -> None:
    """The refusal text is the same, so the two paths cannot drift apart."""
    manifest = _manifest(FILES, "https://evil.example.com/game")
    with pytest.raises(yostar.ManifestRefused) as via_plan:
        yostar.to_plan(manifest)
    with pytest.raises(yostar.ManifestRefused) as via_sync:
        yostar.sync(manifest, tmp_path / "game")
    assert str(via_plan.value) == str(via_sync.value)


# --- end to end: directdl installs a Yostar manifest byte for byte ------------------


@pytest.fixture
def cdn():
    files: dict[str, bytes] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            name = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            data = files.get(name.removeprefix("/game/"))
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield files, f"http://{host}:{port}/game"
    server.shutdown()
    server.server_close()


def test_directdl_installs_the_manifest(cdn, tmp_path: Path) -> None:
    served, base = cdn
    served.update(FILES)
    manifest = _manifest(FILES, base, source=str(tmp_path / "manifest.json"))
    dest = tmp_path / "Arknights_EN"
    _lay_out_disk(dest)
    (dest / "data/c d#e.bin").rmdir()
    directdl.sync(yostar.to_plan(manifest), dest)
    for path, data in FILES.items():
        assert (dest / path).read_bytes() == data, path
    assert yostar.plan(manifest, dest) == []


def test_directdl_refuses_a_file_whose_crc64_differs(cdn, tmp_path: Path) -> None:
    served, base = cdn
    served.update(FILES)
    served["data/a.bin"] = b"z" * 100  # right size, wrong bytes
    manifest = _manifest(FILES, base, source=str(tmp_path / "manifest.json"))
    dest = tmp_path / "Arknights_EN"
    with pytest.raises(directdl.DirectError):
        directdl.sync(yostar.to_plan(manifest), dest)
    assert not (dest / "data/a.bin").exists()
