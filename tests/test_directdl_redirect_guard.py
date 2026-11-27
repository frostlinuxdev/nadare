# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 3a: a plan can carry a redirect allow-list, and directdl honours it.

Yostar's own sync checks every redirect hop (the yo-star CDN, an explicit allow-list,
loopback only from a local manifest). Moving Yostar onto directdl must not lose that, so
``Plan.redirect_allowed`` is passed to every download of the plan's files and archives,
and a plan whose guard could not be applied (streamed archives, Sophon) is refused.
Plans without a guard behave as before.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import socketserver
import threading
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from nadare.core import directdl, sophon, yostar

PAYLOAD = b"game bytes " * 50


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inside.txt", PAYLOAD)
    return buf.getvalue()


ZIP = _zip_bytes()
#: The zip split in two, as a publisher ships a multi-part archive.
PARTS = {"/pack.zip.001": ZIP[: len(ZIP) // 2], "/pack.zip.002": ZIP[len(ZIP) // 2 :]}


@pytest.fixture
def server():
    """``/a.bin`` and ``/pack.zip`` redirect to ``http://localhost:PORT/real/...``."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _send(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            hits.append(path)
            port = self.server.server_address[1]
            if path in ("/a.bin", "/pack.zip", *PARTS):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/real{path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/real/a.bin":
                self._send(PAYLOAD)
            elif path == "/real/pack.zip":
                self._send(ZIP)
            elif path.removeprefix("/real") in PARTS:
                self._send(PARTS[path.removeprefix("/real")])
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    srv.shutdown()
    srv.server_close()


def _no_localhost(url: str) -> bool:
    return urllib.parse.urlparse(url).hostname != "localhost"


def _file_plan(base: str, guard=None) -> directdl.Plan:
    rf = directdl.RemoteFile(
        "a.bin", f"{base}/a.bin", size=len(PAYLOAD), crc64=yostar.crc64(PAYLOAD)
    )
    return directdl.Plan("t", "1", files=(rf,), redirect_allowed=guard)


def _archive_plan(base: str, guard=None) -> directdl.Plan:
    source = directdl.RemoteFile("pack.zip", f"{base}/pack.zip", size=len(ZIP))
    archive = directdl.Archive("pack", source)
    return directdl.Plan("t", "1", archives=(archive,), redirect_allowed=guard)


def test_without_a_guard_redirects_are_followed_as_before(server, tmp_path: Path) -> None:
    base, hits = server
    directdl.sync(_file_plan(base), tmp_path / "game")
    assert (tmp_path / "game/a.bin").read_bytes() == PAYLOAD
    assert "/real/a.bin" in hits


def test_a_guarded_file_download_refuses_the_hop(server, tmp_path: Path) -> None:
    base, hits = server
    with pytest.raises(directdl.DirectError):
        directdl.sync(_file_plan(base, _no_localhost), tmp_path / "game")
    assert not (tmp_path / "game/a.bin").exists()
    assert "/real/a.bin" not in hits


def test_a_guard_that_allows_the_hop_still_downloads(server, tmp_path: Path) -> None:
    base, _ = server
    directdl.sync(_file_plan(base, lambda url: True), tmp_path / "game")
    assert (tmp_path / "game/a.bin").read_bytes() == PAYLOAD


def test_a_guarded_archive_download_refuses_the_hop(server, tmp_path: Path) -> None:
    base, hits = server
    with pytest.raises(directdl.DirectError):
        directdl.sync(_archive_plan(base, _no_localhost), tmp_path / "game")
    assert not (tmp_path / "game/inside.txt").exists()
    assert "/real/pack.zip" not in hits
    # The same plan without the guard installs: the refusal came from the guard.
    directdl.sync(_archive_plan(base), tmp_path / "game2")
    assert (tmp_path / "game2/inside.txt").read_bytes() == PAYLOAD


def test_a_guarded_size_probe_refuses_the_hop(server, tmp_path: Path) -> None:
    """A file without a declared size is probed first; the probe obeys the guard too."""
    base, hits = server
    rf = directdl.RemoteFile("a.bin", f"{base}/a.bin", crc64=yostar.crc64(PAYLOAD))
    plan = directdl.Plan("t", "1", files=(rf,), redirect_allowed=_no_localhost)
    with pytest.raises(directdl.DirectError):
        directdl.sync(plan, tmp_path / "game")
    assert "/real/a.bin" not in hits


def _md5_plan(base: str, guard=None) -> directdl.Plan:
    """A predownload caches files by md5, so its plan must declare one."""
    rf = directdl.RemoteFile(
        "a.bin", f"{base}/a.bin", size=len(PAYLOAD), md5=hashlib.md5(PAYLOAD).hexdigest()
    )
    return directdl.Plan("t", "1", files=(rf,), redirect_allowed=guard)


def test_a_guarded_predownload_refuses_the_hop(server, tmp_path: Path) -> None:
    base, hits = server
    # Control: unguarded, the same plan predownloads, so a refusal below is the guard's.
    directdl.predownload(_md5_plan(base), tmp_path / "game", tmp_path / "cache0")
    hits.clear()
    with pytest.raises(directdl.DirectError):
        directdl.predownload(_md5_plan(base, _no_localhost), tmp_path / "game", tmp_path / "cache")
    assert "/real/a.bin" not in hits


def _parts_plan(base: str, guard=None) -> directdl.Plan:
    source = directdl.RemoteFile("pack.zip", f"{base}/pack.zip", size=len(ZIP))
    parts = tuple(
        directdl.RemoteFile(name.lstrip("/"), f"{base}{name}", size=len(data))
        for name, data in PARTS.items()
    )
    archive = directdl.Archive("pack", source, parts=parts)
    return directdl.Plan("t", "1", archives=(archive,), redirect_allowed=guard)


def test_a_guarded_multipart_archive_refuses_the_hop(server, tmp_path: Path) -> None:
    base, hits = server
    directdl.sync(_parts_plan(base), tmp_path / "ok")
    assert (tmp_path / "ok/inside.txt").read_bytes() == PAYLOAD
    hits.clear()
    with pytest.raises(directdl.DirectError):
        directdl.sync(_parts_plan(base, _no_localhost), tmp_path / "game")
    assert not any(hit.startswith("/real/") for hit in hits)


def test_a_guard_on_a_streamed_archive_is_refused() -> None:
    source = directdl.RemoteFile("pack.zip", "https://cdn.yo-star.com/pack.zip", size=10)
    archive = directdl.Archive("pack", source, stream=True)
    plan = directdl.Plan("t", "1", archives=(archive,), redirect_allowed=_no_localhost)
    with pytest.raises(directdl.DirectError, match="redirect"):
        directdl._validate_plan(plan)
    # Unguarded, the same plan is fine.
    directdl._validate_plan(directdl.Plan("t", "1", archives=(archive,)))


def test_a_guard_on_sophon_packages_is_refused() -> None:
    pkg = sophon.SophonPackage(
        name="game",
        manifest_url="https://cdn.example.com/m",
        chunk_url="https://cdn.example.com/c",
        manifest_id="x",
        size=1,
    )
    plan = directdl.Plan("t", "1", sophon=(pkg,), redirect_allowed=_no_localhost)
    with pytest.raises(directdl.DirectError, match="redirect"):
        directdl._validate_plan(plan)


def test_the_guard_does_not_change_plan_equality() -> None:
    """A plan is compared by what it installs, not by which guard object it carries."""
    base = "https://cdn.yo-star.com/g"
    assert _file_plan(base, _no_localhost) == _file_plan(base, lambda url: True)


# --- Yostar plans carry Yostar's guard ------------------------------------------------


def _manifest(base_url: str, source: str) -> yostar.Manifest:
    return yostar.Manifest(
        game_tag="Arknights_EN",
        version="1",
        base_url=base_url,
        generated_at="",
        files=(yostar.ManifestFile("a.bin", len(PAYLOAD), yostar.crc64(PAYLOAD)),),
        source=source,
    )


def test_yostar_plan_guard_keeps_yostar_hosts() -> None:
    plan = yostar.to_plan(
        _manifest("https://cdn.yo-star.com/g", "https://cdn.yo-star.com/index.json")
    )
    guard = plan.redirect_allowed
    assert guard is not None
    assert guard("https://other.yo-star.com/g/a.bin")
    assert not guard("https://evil.example.com/a.bin")
    assert not guard("http://127.0.0.1:8080/a.bin")  # remote manifest: no loopback hop


def test_yostar_plan_guard_honours_allowed_hosts() -> None:
    plan = yostar.to_plan(
        _manifest("https://mirror.example.net/g", "https://mirror.example.net/index.json"),
        allowed_hosts={"mirror.example.net"},
    )
    assert plan.redirect_allowed("https://mirror.example.net/x")
    assert not plan.redirect_allowed("https://evil.example.com/x")


def test_yostar_plan_guard_allows_loopback_for_a_local_manifest() -> None:
    plan = yostar.to_plan(_manifest("http://127.0.0.1:9/g", "/tmp/manifest.json"))
    assert plan.redirect_allowed("http://localhost:9/real/a.bin")


def test_yostar_install_through_directdl_refuses_an_off_list_hop(server, tmp_path: Path) -> None:
    """End to end: a Yostar manifest whose CDN bounces to a host off the list installs nothing."""
    base, hits = server
    plan = yostar.to_plan(_manifest(base, str(tmp_path / "manifest.json")))
    # localhost is loopback and the manifest is local, so Yostar's rule allows the hop...
    directdl.sync(plan, tmp_path / "ok")
    assert (tmp_path / "ok/a.bin").read_bytes() == PAYLOAD
    # ...and a guard that refuses it is obeyed.
    narrowed = directdl.Plan(
        plan.game_tag, plan.version, files=plan.files, redirect_allowed=_no_localhost
    )
    hits.clear()
    with pytest.raises(directdl.DirectError):
        directdl.sync(narrowed, tmp_path / "refused")
    assert "/real/a.bin" not in hits


def test_a_refused_hop_is_not_retried(server, tmp_path: Path) -> None:
    """The guard's answer cannot change on a second try: fail at once, ask the server once.

    Every refusal used to go round the download retry loop with backoff, so the user waited
    about half a minute for the same "refused" several times over (2026-11-27).
    """
    import time

    base, hits = server
    start = time.monotonic()
    with pytest.raises(directdl.DirectError):
        directdl.sync(_file_plan(base, _no_localhost), tmp_path / "game")
    assert hits.count("/a.bin") == 1, hits
    assert time.monotonic() - start < 3
