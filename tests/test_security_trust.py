# SPDX-License-Identifier: GPL-3.0-or-later
"""Trust boundaries: where game files may come from, which links may open."""

from __future__ import annotations

import pytest

from nadare.core import yostar


def _man(base: str, payload: bytes = b"x") -> yostar.Manifest:
    return yostar.Manifest(
        game_tag="StellaSora_EN",
        version="1",
        base_url=base,
        files=(yostar.ManifestFile("x.bin", len(payload), yostar.crc64(payload)),),
        generated_at="",
        source="test",
    )


class _NoDownload:
    def __call__(self, *a, **k):
        raise AssertionError("NO-DOWNLOAD-EXPECTED")


class TestYostarDownloadTrust:
    def test_manifest_cannot_vouch_for_its_own_host(self):
        assert not yostar.host_allowed("mirror.example", "mirror.example")
        assert not yostar.host_allowed("evil.example", "evil.example", None)

    def test_yostar_hosts_and_explicit_allowlist_still_work(self):
        assert yostar.host_allowed("launcher-pkg-ss-en.yo-star.com", "x")
        assert yostar.host_allowed("yo-star.com", "x")
        assert not yostar.host_allowed("yo-star.com.evil.example", "x")
        assert not yostar.host_allowed("evilyo-star.com", "x")
        assert yostar.host_allowed("mirror.example", "x", {"mirror.example"})

    def test_sync_refuses_a_foreign_base_host_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yostar.download, "stream_download", _NoDownload())
        with pytest.raises(yostar.ManifestError, match="refusing to download from untrusted host"):
            yostar.sync(_man("https://evil.example/assets"), tmp_path)
        assert not (tmp_path / "x.bin").exists()

    def test_sync_refuses_plain_http_even_from_yostar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yostar.download, "stream_download", _NoDownload())
        with pytest.raises(yostar.ManifestError, match="https"):
            yostar.sync(_man("http://launcher-pkg-ss-en.yo-star.com/assets"), tmp_path)

    def test_plain_http_refused_for_an_allowlisted_mirror_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yostar.download, "stream_download", _NoDownload())
        with pytest.raises(yostar.ManifestError, match="https"):
            yostar.sync(
                _man("http://mirror.example/assets"), tmp_path, allowed_hosts={"mirror.example"}
            )

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
    def test_loopback_http_is_allowed_for_local_test_servers(self, host):
        assert yostar.base_url_trusted(f"http://{host}:8123/assets")

    @pytest.mark.parametrize(
        ("url", "ok"),
        [
            ("https://launcher-pkg-ss-en.yo-star.com/a", True),
            ("https://cdn.yo-star.com", True),
            ("http://cdn.yo-star.com/a", False),
            ("https://evil.example/a", False),
            ("ftp://cdn.yo-star.com/a", False),
            ("https://u:p@cdn.yo-star.com/a", False),
            ("", False),
        ],
    )
    def test_base_url_trusted(self, url, ok):
        assert yostar.base_url_trusted(url) is ok

    def test_base_url_trusted_with_allowlist(self):
        assert yostar.base_url_trusted("https://mirror.example/a", {"mirror.example"})
        assert not yostar.base_url_trusted("http://mirror.example/a", {"mirror.example"})


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6")
    # Without a display Qt aborts the interpreter before the link-allowlist
    # negative cases run; offscreen needs no X/Wayland server (#82).
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class TestNewsLinks:
    @pytest.fixture
    def view(self, qapp):
        from nadare.gui.game_view import GameView

        view = GameView()
        yield view
        view.deleteLater()

    @pytest.mark.parametrize(
        ("url", "opens"),
        [
            ("https://fate-go.us/news/?article=x", True),
            ("http://fate-go.us/news/?article=x", True),
            ("file:///etc/passwd", False),
            ("steam://run/12345", False),
            ("smb://host/share", False),
            ("javascript:alert(1)", False),
            ("/home/user/.ssh/id_ed25519", False),
            ("", False),
        ],
    )
    def test_only_web_links_open(self, view, monkeypatch, url, opens):
        from nadare.gui import game_view

        opened = []
        monkeypatch.setattr(game_view.QDesktopServices, "openUrl", lambda u: opened.append(u))
        view._open_external(url)
        assert bool(opened) is opens
