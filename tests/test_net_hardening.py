# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-2 audit: network hardening against hostile or broken servers."""

from __future__ import annotations

import http.server
import io
import os
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import artwork, download, runners, yostar


class _Server:
    """Loopback server: ``/big`` streams ``size`` bytes, ``/r1`` redirects to ``/r2``."""

    def __init__(self, size: int = 0) -> None:
        self.sent = 0
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                if self.path == "/r1":
                    self.send_response(302)
                    self.send_header("Location", "/r2")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                block = b"x" * 65536
                try:
                    for _ in range(size // 65536):
                        self.wfile.write(b"%x\r\n" % len(block) + block + b"\r\n")
                        outer.sent += len(block)
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made: list[_Server] = []

    def make(size: int = 0) -> _Server:
        s = _Server(size)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


# --- 2-F2 ------------------------------------------------------------------
def test_stream_download_aborts_early(server, tmp_path):
    srv = server(40 << 20)
    pool = download.HttpClientPool()
    out = tmp_path / "d"
    out.mkdir()
    dest = out / "f.bin"
    with pytest.raises(download.DownloadError):
        download.stream_download(srv.url + "/big", dest, pool=pool, expected_size=10)
    pool.close()
    # ``sent`` counts bytes the server wrote into loopback socket buffers, which
    # can legitimately hold a few MiB before the abort reaches it; the point is
    # that it did not stream the whole 40 MiB.
    assert srv.sent < (16 << 20)
    assert not list(out.iterdir())


def test_download_aborts_early_and_removes_part(server, tmp_path):
    srv = server(40 << 20)
    out = tmp_path / "d"
    out.mkdir()
    dest = out / "f.bin"
    with pytest.raises(download.DownloadError):
        download.download(srv.url + "/big", dest, expected_size=10, retries=1)
    assert srv.sent < (16 << 20)
    assert not list(out.iterdir())


def test_http_get_is_capped(monkeypatch):
    class R:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * (n if n and n > 0 else 1 << 30)

    monkeypatch.setattr(download, "http_open", lambda url, **kw: R())
    monkeypatch.setattr(download, "MAX_TEXT_BYTES", 1024)
    with pytest.raises(download.DownloadError):
        download.http_get("https://x.example/a")


# --- 2-F3 ------------------------------------------------------------------
def test_download_refuses_offhost_redirect(server, tmp_path):
    srv = server()
    with pytest.raises(download.DownloadError):
        download.download(
            srv.url + "/r1", tmp_path / "a", retries=1, redirect_allowed=lambda u: False
        )
    assert srv.hits == ["/r1"]


def test_runner_install_passes_redirect_allowlist(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_download(url, dest, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners.paths, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "r")
    monkeypatch.setattr(runners.diskspace, "check_space", lambda *a, **k: None)
    monkeypatch.setattr(
        runners,
        "_release",
        lambda spec, tag: {
            "tag": "t",
            "tar": {
                "name": "a.tar.gz",
                "size": 5,
                "browser_download_url": "https://github.com/a/b/a.tar.gz",
                "digest": "sha256:" + "0" * 64,
            },
            "sum": None,
        },
    )
    rid = next(iter(runners.RUNNERS))
    with pytest.raises(RuntimeError):
        runners.install(rid, latest=True)
    assert seen.get("redirect_allowed") is runners._github_asset_host_allowed


# --- 2-F4 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "0x7f.0.0.1",
        "0177.0.0.1",
        "10.1",
        "2130706433",
        "[::1]",
        "[::ffff:127.0.0.1]",
    ],
)
def test_numeric_local_hosts_rejected(host):
    assert not artwork.host_allowed(f"https://{host}/x.png", ())
    assert not yostar.media_url_allowed(f"https://{host}/x.png")


def test_public_host_still_allowed():
    assert artwork.host_allowed("https://cdn.example.com/x.png", ())


def test_fetch_skips_non_https_and_numeric_hosts(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        artwork, "_candidates", lambda *a, **k: iter(["http://127.0.0.1/x.png", "https://127.1/x.png"])
    )
    monkeypatch.setattr(artwork, "_download", lambda url: calls.append(url) or (b"x", "png"))
    assert artwork.fetch({"name": "g"}, "g") is None
    assert calls == []


# --- 2-F5 ------------------------------------------------------------------
def test_sync_rejects_symlinked_dir(tmp_path, monkeypatch):
    root = tmp_path / "root"
    out = tmp_path / "out"
    root.mkdir()
    out.mkdir()
    (root / "link").symlink_to(out)
    payload = b"hello"

    def fake(url, dest, **kw):
        Path(dest).write_bytes(payload)
        h = kw.get("hasher")
        if h is not None:
            h.update(payload)
        return download.StreamResult(Path(dest), len(payload))

    monkeypatch.setattr(yostar.download, "stream_download", fake)
    man = yostar.Manifest(
        "t", "1", "https://cdn.yo-star.com/a", "",
        (yostar.ManifestFile("link/x", len(payload), yostar.crc64(payload)),),
    )
    with pytest.raises(yostar.ManifestError):
        yostar.sync(man, root, workers=1, retries=1)
    assert list(out.iterdir()) == []


# --- 2-F6 ------------------------------------------------------------------
def test_news_labels_plaintext():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import ElidedLabel, NewsCard

    app = QApplication.instance() or QApplication([])
    assert app is not None
    label = ElidedLabel('<img src="/etc/hostname"><b>x</b>')
    assert label.textFormat() == Qt.TextFormat.PlainText
    card = NewsCard({"id": "1", "title": "<b>t</b>", "category": "<i>c</i>", "summary": ""})
    assert card.title_label.textFormat() == Qt.TextFormat.PlainText
    assert card.category_label.textFormat() == Qt.TextFormat.PlainText


# --- 2-F7 ------------------------------------------------------------------
def test_webview2_redirect_checked_before_following(monkeypatch):
    seen: dict = {}

    def fake_open(url, *, headers=None, timeout=60, redirect_allowed=None):
        seen["cb"] = redirect_allowed
        raise download.DownloadError("stop")

    from nadare.backends.czn import backend as czn_backend

    monkeypatch.setattr(download, "http_open", fake_open)
    b = czn_backend.CznBackend.__new__(czn_backend.CznBackend)
    b.ctx = SimpleNamespace(emit=lambda event: None, cancel=object())
    with pytest.raises(czn_backend.BackendError):
        b._download_webview2(
            "https://go.microsoft.com/x", Path("/nonexistent/t"), Path("/nonexistent/i")
        )
    cb = seen["cb"]
    assert cb("https://x.microsoft.com/a")
    assert not cb("http://x.microsoft.com/a")
    assert not cb("https://evil.example/a")


def test_vcredist_redirect_checked_before_following(monkeypatch, tmp_path):
    from nadare.core import vcredist

    seen: dict = {}

    def fake_open(url, *, headers=None, timeout=60, redirect_allowed=None):
        seen["cb"] = redirect_allowed
        raise download.DownloadError("stop")

    monkeypatch.setattr(download, "http_open", fake_open)
    with pytest.raises(vcredist.BackendError):
        vcredist.download(  # type: ignore[call-arg]
            "https://aka.ms/vc.exe", tmp_path / "t", tmp_path / "i",
            verify_fn=lambda p: None,
            emit=lambda e: None,
            cancel=type("C", (), {"cancelled": False})(),
        )
    cb = seen["cb"]
    assert cb("https://download.microsoft.com/a")
    assert not cb("http://download.microsoft.com/a")
    assert not cb("https://evil.example/a")


# --- 3-F1 ------------------------------------------------------------------
def test_prune_stale_keeps_fresh_unpack(tmp_path):
    live = tmp_path / ".unpack-live"
    live.mkdir()
    runners._prune_stale(tmp_path)
    assert live.exists()
    old = time.time() - 7 * 3600
    os.utime(live, (old, old))
    runners._prune_stale(tmp_path)
    assert not live.exists()


def test_truncated_extraction_is_not_installed(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    tar = tmp_path / "r.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for name in ("R/proton", "R/files/a", "R/files/b"):
            data = b"x"
            info = tarfile.TarInfo(name)
            info.size = 1
            t.addfile(info, io.BytesIO(data))
    real_extract = tarfile.TarFile.extract

    def flaky(self, member, path="", **kw):
        if getattr(member, "name", member) == "R/files/b":
            return  # simulate a concurrent prune wiping part of the tree
        return real_extract(self, member, path, **kw)

    monkeypatch.setattr(tarfile.TarFile, "extract", flaky)
    with pytest.raises(runners.RunnerError):
        runners._extract(tar, cancel=None, emit=lambda e: None)
    assert not (root / "R").exists()


# ---------------------------------------------------------------------------
# a redirect's body is drained under a cap, never read whole (#43)
# ---------------------------------------------------------------------------
def test_refused_redirect_drains_a_bounded_amount():
    from nadare.core import download

    class FakeFp:
        def __init__(self):
            self.sizes: list[int | None] = []
            self.closed = False

        def read(self, size=None):
            self.sizes.append(size)
            return b""

        def close(self):
            self.closed = True

    fp = FakeFp()
    handler = download._SafeRedirectHandler()
    with pytest.raises(download.DownloadError):
        handler._refuse(fp, "refused")
    assert fp.closed is True
    assert fp.sizes and all(s is not None and 0 < s <= (1 << 20) for s in fp.sizes), fp.sizes


def test_pool_redirect_with_a_huge_body_is_not_buffered(tmp_path):
    """The 3xx hop advertises a 200 MiB body; the pool must follow the redirect
    without reading it whole (the server notices the early close)."""
    import http.server
    import threading

    from nadare.core import download

    sent = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/ok":
                body = b"final"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", str(200 << 20))
            self.end_headers()
            block = b"x" * 65536
            try:
                for _ in range((200 << 20) // 65536):
                    self.wfile.write(block)
                    sent["n"] += len(block)
            except OSError:
                pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        pool = download.HttpClientPool()
        resp = pool.open(f"http://127.0.0.1:{httpd.server_address[1]}/start")
        assert resp.read() == b"final"
        pool.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert sent["n"] < (64 << 20), sent["n"]
