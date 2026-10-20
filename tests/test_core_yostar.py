# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for :mod:`nadare.core.yostar`.

No socket is opened: ``download.download`` is monkeypatched, every path lives
under the isolated ``tmp_path`` tree, and the only salt used is ``"test-salt"``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nadare.core import download as download_mod
from nadare.core import yostar


# ---------------------------------------------------------------------------
# CRC-64/XZ
# ---------------------------------------------------------------------------
class TestCrc64:
    def test_standard_check_value(self):
        assert yostar.crc64(b"123456789") == 0x995DC9BBDF1939FA

    def test_empty(self):
        # CRC-64/XZ of the empty string is 0 with init/xorout all-ones.
        assert yostar.crc64(b"") == 0

    def test_file_matches_bytes(self, tmp_path):
        data = b"the quick brown fox" * 1000
        p = tmp_path / "f.bin"
        p.write_bytes(data)
        assert yostar.crc64_file(p) == yostar.crc64(data)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0x995dc9bbdf1939fa", 0x995DC9BBDF1939FA),
            ("995DC9BBDF1939FA", 0x995DC9BBDF1939FA),
            ("11051210869376104954", 0x995DC9BBDF1939FA),
            (0x995DC9BBDF1939FA, 0x995DC9BBDF1939FA),
            ("0", 0),
            ("", 0),
            (None, 0),
        ],
    )
    def test_normalise(self, raw, expected):
        assert yostar.normalize_crc64(raw) == expected

    @pytest.mark.parametrize("raw", ["0xZZ", "not-a-number", "12 34"])
    def test_normalise_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            yostar.normalize_crc64(raw)

    def test_python_and_native_backends_agree(self):
        import os

        data = os.urandom(200_000)
        py = yostar._crc64_python(data)
        if yostar._NATIVE_CRC64 is not None:
            assert yostar._crc64_native(data) == py
        assert yostar.crc64(data) == py

    def test_native_matches_check_value(self):
        if yostar._NATIVE_CRC64 is None:
            pytest.skip("liblzma not available")
        assert yostar._crc64_native(b"123456789") == 0x995DC9BBDF1939FA

    def test_python_fallback_when_native_missing(self, monkeypatch):
        monkeypatch.setattr(yostar, "_NATIVE_CRC64", None)
        assert yostar.crc64(b"123456789") == 0x995DC9BBDF1939FA
        assert yostar._crc64_python(b"") == 0

    def test_incremental_helper(self):
        data = b"the quick brown fox" * 99
        hasher = yostar.Crc64()
        hasher.update(data[:100]).update(data[100:])
        assert hasher.value == yostar.crc64(data)
        hasher.reset()
        assert hasher.value == 0

    def test_incremental_python_fallback(self, monkeypatch):
        monkeypatch.setattr(yostar, "_NATIVE_CRC64", None)
        hasher = yostar.Crc64(yostar.crc64(b"abc"))
        hasher.update(b"def")
        assert hasher.value == yostar.crc64(b"abcdef")


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------
class TestSigning:
    def test_head_key_order_and_sign(self):
        head = yostar.build_head("StellaSora_EN", "1.2.3", time=1_700_000_000)
        assert list(head) == ["game_tag", "time", "version"]
        head_str = yostar.head_string(head)
        assert head_str == '{"game_tag":"StellaSora_EN","time":1700000000,"version":"1.2.3"}'
        expected = hashlib.md5((head_str + "" + "test-salt").encode()).hexdigest()
        assert yostar.signature(head_str, "", "test-salt") == expected

    def test_data_str_is_included(self):
        head_str = yostar.head_string(yostar.build_head("Arknights_EN", "9.9", time=123))
        expected = hashlib.md5((head_str + '{"x":1}' + "test-salt").encode()).hexdigest()
        assert yostar.signature(head_str, '{"x":1}', "test-salt") == expected

    def test_authorization_header_shape(self):
        header = yostar.authorization_header(
            "StellaSora_EN", "1.2.3", "test-salt", time=1_700_000_000
        )
        parsed = json.loads(header)
        assert list(parsed) == ["head", "sign"]
        assert list(parsed["head"]) == ["game_tag", "time", "version"]
        assert "test-salt" not in header
        assert parsed["head"]["time"] == 1_700_000_000

    def test_signed_headers(self):
        headers = yostar.signed_headers("Arknights_EN", "1.2.3", "test-salt", time=5)
        assert headers["Accept"] == "application/json, text/plain, */*"
        assert "Authorization" in headers
        assert "test-salt" not in headers["Authorization"]


# ---------------------------------------------------------------------------
# manifest loading / shape
# ---------------------------------------------------------------------------
def _manifest(**over):
    base = {
        "game_tag": "StellaSora_EN",
        "version": "1.0.0",
        "base_url": "https://cdn.yo-star.com/assets",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": "StellaSora.exe", "size": 3, "crc64": yostar.crc64(b"abc")},
            {"path": "sub/data.bin", "size": 1, "crc64": yostar.crc64(b"z")},
        ],
    }
    base.update(over)
    return base


def _write(tmp_path: Path, payload) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestLoadManifest:
    def test_loads_local_file(self, tmp_path):
        path = _write(tmp_path, _manifest())
        man = yostar.load_manifest(str(path))
        assert man.game_tag == "StellaSora_EN"
        assert man.version == "1.0.0"
        assert man.base_url == "https://cdn.yo-star.com/assets"
        assert [f.path for f in man.files] == ["StellaSora.exe", "sub/data.bin"]
        assert man.files[0].crc64 == yostar.crc64(b"abc")

    def test_loads_https_with_fake_fetch(self, tmp_path, monkeypatch):
        payload = json.dumps(_manifest()).encode()
        monkeypatch.setattr(yostar.download, "http_get", lambda url, **kw: payload)
        man = yostar.load_manifest("https://manifests.example/yostar/StellaSora_EN.json")
        assert man.version == "1.0.0"

    def test_manifest_fetch_forwards_a_redirect_guard(self, monkeypatch):
        captured: dict = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs)
            return json.dumps(_manifest()).encode()

        monkeypatch.setattr(yostar.download, "http_get", fake_get)
        yostar.load_manifest("https://manifests.example/yostar/StellaSora_EN.json")

        guard = captured.get("redirect_allowed")
        assert guard is not None
        assert guard("https://cdn.yo-star.com/a")
        assert not guard("https://evil.example/a")
        # A remote manifest may not redirect its own fetch at localhost.
        assert not guard("http://127.0.0.1/a")

    def test_normalises_hex_and_decimal_crc(self, tmp_path):
        payload = _manifest(
            files=[
                {"path": "a", "size": "9", "crc64": "0x995dc9bbdf1939fa"},
                {"path": "b", "size": 1, "crc64": "123"},
            ]
        )
        man = yostar.load_manifest(str(_write(tmp_path, payload)))
        assert man.files[0].size == 9
        assert man.files[0].crc64 == 0x995DC9BBDF1939FA
        assert man.files[1].crc64 == 123

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../evil", "a/../../evil", "..", ""])
    def test_rejects_escaping_paths(self, tmp_path, bad):
        payload = _manifest(files=[{"path": bad, "size": 1, "crc64": 1}])
        with pytest.raises(yostar.ManifestError):
            yostar.load_manifest(str(_write(tmp_path, payload)))

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"files": []},
            {"game_tag": 1, "version": "v", "base_url": "https://a", "files": []},
            {"game_tag": "t", "version": "v", "base_url": "", "files": []},
            {"game_tag": "t", "version": "v", "base_url": "https://a", "files": "no"},
            {"game_tag": "t", "version": "v", "base_url": "https://a", "files": [{"path": 1}]},
        ],
    )
    def test_rejects_bad_shape(self, tmp_path, payload):
        with pytest.raises(yostar.ManifestError):
            yostar.load_manifest(str(_write(tmp_path, payload)))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(yostar.ManifestError):
            yostar.load_manifest(str(tmp_path / "nope.json"))

    def test_index_is_resolved_for_tag(self, tmp_path, monkeypatch):
        leaf = _manifest()
        index = {
            "StellaSora_EN": {
                "version": "1.0.0",
                "url": "https://manifests.example/yostar/StellaSora_EN.json",
                "sha256": "x",
            }
        }
        index_path = tmp_path / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")

        def fake_get(url, **kw):
            assert url == "https://manifests.example/yostar/StellaSora_EN.json"
            return json.dumps(leaf).encode()

        monkeypatch.setattr(yostar.download, "http_get", fake_get)
        man = yostar.load_manifest(str(index_path), game_tag="StellaSora_EN")
        assert man.version == "1.0.0"

    def test_relative_index_url_resolves_against_file(self, tmp_path):
        leaf = _manifest()
        (tmp_path / "StellaSora_EN.json").write_text(json.dumps(leaf), encoding="utf-8")
        index = {"StellaSora_EN": {"version": "1", "url": "StellaSora_EN.json", "sha256": "x"}}
        index_path = tmp_path / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        man = yostar.load_manifest(str(index_path), game_tag="StellaSora_EN")
        assert man.base_url == leaf["base_url"]


class TestManifestSource:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("YOSTAR_MANIFEST_INDEX", "https://env/index.json")
        assert yostar.configured_manifest({"manifest_index": "https://profile/x"}) == (
            "https://env/index.json"
        )

    def test_profile_index_then_file(self):
        assert yostar.configured_manifest({"manifest_index": "https://p/x"}) == "https://p/x"
        assert yostar.configured_manifest({"manifest_file": "/tmp/m.json"}) == "/tmp/m.json"

    def test_extra_mapping(self):
        prof = {"extra": {"manifest_file": "/tmp/extra.json"}}
        assert yostar.configured_manifest(prof) == "/tmp/extra.json"

    def test_empty_default(self):
        assert yostar.configured_manifest({}) == ""


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
class TestPlan:
    def _man(self, files):
        return yostar.Manifest(
            game_tag="t",
            version="1",
            base_url="https://cdn.yo-star.com/a",
            generated_at="",
            files=tuple(files),
        )

    def test_missing_wrong_size_wrong_crc(self, tmp_path):
        good = tmp_path / "good.bin"
        good.write_bytes(b"abc")
        wrong_size = tmp_path / "wrong.bin"
        wrong_size.write_bytes(b"ab")
        wrong_crc = tmp_path / "crc.bin"
        wrong_crc.write_bytes(b"abd")
        missing = "new.bin"
        files = [
            yostar.ManifestFile("good.bin", 3, yostar.crc64(b"abc")),
            yostar.ManifestFile("wrong.bin", 3, yostar.crc64(b"abc")),
            yostar.ManifestFile("crc.bin", 3, yostar.crc64(b"abc")),
            yostar.ManifestFile(missing, 4, yostar.crc64(b"abcd")),
        ]
        todo = yostar.plan(self._man(files), tmp_path)
        assert [f.path for f in todo] == ["wrong.bin", "crc.bin", "new.bin"]

    def test_up_to_date_is_empty(self, tmp_path):
        (tmp_path / "ok.bin").write_bytes(b"hello")
        files = [yostar.ManifestFile("ok.bin", 5, yostar.crc64(b"hello"))]
        assert yostar.plan(self._man(files), tmp_path) == []


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
class _FakeDownload:
    """Minimal stand-in for ``download.stream_download`` (resume + hash)."""

    def __init__(self, contents: dict[str, bytes], *, on_call=None):
        self.contents = contents
        self.calls: list[tuple[str, Path, bool]] = []
        self.on_call = on_call

    def __call__(self, url, dest, **kwargs):
        dest = Path(dest)
        self.calls.append((url, dest, bool(kwargs.get("resume", True))))
        if self.on_call is not None:
            self.on_call(url, dest)
        data = self.contents[url]
        part = dest.with_name(dest.name + ".part")
        offset = part.stat().st_size if part.exists() else 0
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "ab") as fh:
            fh.write(data[offset:])
        hasher = kwargs.get("hasher")
        if hasher is not None:
            hasher.reset()
            hasher.update(part.read_bytes())
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(len(data), len(data), len(data))
        return download_mod.StreamResult(dest, len(data), resumed=offset > 0)


class TestSync:
    def _man(self, files, base="https://cdn.yo-star.com/assets"):
        return yostar.Manifest(
            game_tag="t",
            version="1",
            base_url=base,
            generated_at="",
            files=tuple(files),
        )

    def test_downloads_and_verifies(self, tmp_path, monkeypatch):
        payload = b"hello world"
        man = self._man([yostar.ManifestFile("a/b.bin", len(payload), yostar.crc64(payload))])
        fake = _FakeDownload({"https://cdn.yo-star.com/assets/a/b.bin": payload})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        events = []
        summary = yostar.sync(man, tmp_path, emit=events.append)
        assert (tmp_path / "a" / "b.bin").read_bytes() == payload
        assert summary["downloaded"] == 1
        assert events[-1].progress == 1.0

    def test_checks_space_for_missing_bytes(self, tmp_path, monkeypatch):
        payload = b"hello world"
        man = self._man([yostar.ManifestFile("a.bin", len(payload), yostar.crc64(payload))])
        fake = _FakeDownload({"https://cdn.yo-star.com/assets/a.bin": payload})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        calls: list[tuple[Path, int]] = []
        monkeypatch.setattr(
            yostar.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )
        yostar.sync(man, tmp_path)
        assert calls == [(tmp_path, len(payload))]

    def test_disk_check_subtracts_partial_bytes(self, tmp_path, monkeypatch):
        payload = b"0123456789"
        dest = tmp_path / "resume.bin"
        dest.with_name("resume.bin.part").write_bytes(payload[:4])
        man = self._man([yostar.ManifestFile("resume.bin", len(payload), yostar.crc64(payload))])
        fake = _FakeDownload({"https://cdn.yo-star.com/assets/resume.bin": payload})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        calls: list[tuple[Path, int]] = []
        monkeypatch.setattr(
            yostar.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )
        yostar.sync(man, tmp_path)
        assert calls == [(tmp_path, len(payload) - 4)]

    def test_resumes_part_file(self, tmp_path, monkeypatch):
        payload = b"0123456789"
        dest = tmp_path / "resume.bin"
        dest.with_name("resume.bin.part").write_bytes(payload[:4])
        man = self._man([yostar.ManifestFile("resume.bin", len(payload), yostar.crc64(payload))])
        fake = _FakeDownload({"https://cdn.yo-star.com/assets/resume.bin": payload})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        yostar.sync(man, tmp_path)
        assert dest.read_bytes() == payload
        assert fake.calls[0][2] is True

    def test_crc_mismatch_retries_then_fails(self, tmp_path, monkeypatch):
        man = self._man([yostar.ManifestFile("bad.bin", 3, yostar.crc64(b"abc"))])
        fake = _FakeDownload({"https://cdn.yo-star.com/assets/bad.bin": b"XYZ"})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        with pytest.raises(yostar.ManifestError):
            yostar.sync(man, tmp_path, retries=2)
        assert len(fake.calls) == 2
        assert not (tmp_path / "bad.bin").exists()

    def test_rejects_foreign_host(self, tmp_path, monkeypatch):
        man = self._man(
            [yostar.ManifestFile("x.bin", 1, 0)],
            base="https://evil.example/assets",
        )
        # A manifest whose base host is not yo-star is only trusted if the
        # profile explicitly points at it; the default allowlist rejects it.
        with pytest.raises(yostar.ManifestError):
            yostar.sync(man, tmp_path, allowed_hosts={"cdn.yo-star.com"})

    def test_allows_exact_base_host(self, tmp_path, monkeypatch):
        payload = b"x"
        man = self._man(
            [yostar.ManifestFile("x.bin", 1, yostar.crc64(payload))],
            base="https://mirror.example/assets",
        )
        fake = _FakeDownload({"https://mirror.example/assets/x.bin": payload})
        monkeypatch.setattr(yostar.download, "stream_download", fake)
        yostar.sync(man, tmp_path, allowed_hosts={"mirror.example"})
        assert (tmp_path / "x.bin").read_bytes() == payload

    def test_yo_star_suffix_is_allowed(self):
        assert yostar.host_allowed("cdn.yo-star.com", "cdn.yo-star.com")
        assert yostar.host_allowed("anything.yo-star.com", "cdn.yo-star.com")
        assert not yostar.host_allowed("evil.com", "cdn.yo-star.com")

    def test_sync_forwards_a_redirect_guard(self, tmp_path, monkeypatch):
        """05-4: an allowlisted file host must not redirect to an unlisted host."""
        payload = b"x"
        man = yostar.Manifest(
            game_tag="t",
            version="1",
            base_url="https://cdn.yo-star.com/assets",
            generated_at="",
            source="https://index.example/index.json",
            files=(yostar.ManifestFile("x.bin", 1, yostar.crc64(payload)),),
        )
        captured: dict = {}

        def fake_stream(url, dest, **kwargs):
            captured["guard"] = kwargs.get("redirect_allowed")
            part = Path(dest).with_name(Path(dest).name + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(payload)
            hasher = kwargs.get("hasher")
            if hasher is not None:
                hasher.reset()
                hasher.update(payload)
            return download_mod.StreamResult(Path(dest), len(payload))

        monkeypatch.setattr(yostar.download, "stream_download", fake_stream)
        yostar.sync(man, tmp_path, workers=1)

        guard = captured["guard"]
        assert guard is not None
        assert guard("https://cdn.yo-star.com/other")
        assert guard("https://launcher-pkg-ss-en.yo-star.com/other")
        assert not guard("https://evil.example/other")
        # The remote manifest may not bounce the download to loopback.
        assert not guard("http://127.0.0.1/other")


# ---------------------------------------------------------------------------
# official media
# ---------------------------------------------------------------------------
def _media(**over):
    base = {
        "game_tag": "Arknights_EN",
        "generated_at": "2026-01-01T00:00:00Z",
        "background": "https://launcher-pkg-ark-en.yo-star.com/img/bg.jpg",
        "banners": [
            {
                "image": "https://launcher-pkg-ark-en.yo-star.com/img/b1.png",
                "url": "https://www.arknights.global/news/1",
            },
            {"image": ""},
        ],
    }
    base.update(over)
    return base


class TestLoadMedia:
    def test_loads_local_file(self, tmp_path):
        path = tmp_path / "media.json"
        path.write_text(json.dumps(_media()))
        media = yostar.load_media(str(path))
        assert media.game_tag == "Arknights_EN"
        assert media.background.endswith("bg.jpg")
        assert [(b.image, b.url) for b in media.banners] == [
            (
                "https://launcher-pkg-ark-en.yo-star.com/img/b1.png",
                "https://www.arknights.global/news/1",
            )
        ]

    def test_loads_from_manifest_index_media_entry(self, tmp_path, monkeypatch):
        (tmp_path / "Arknights_EN.media.json").write_text(json.dumps(_media()))
        index = {
            "Arknights_EN": {
                "version": "1",
                "url": "Arknights_EN.json",
                "media": "Arknights_EN.media.json",
                "sha256": "x",
            }
        }
        index_path = tmp_path / "index.json"
        index_path.write_text(json.dumps(index))
        media = yostar.load_media(str(index_path), game_tag="Arknights_EN")
        assert media.background.endswith("bg.jpg")

    def test_index_without_media_raises(self, tmp_path):
        index = {"X": {"version": "1", "url": "X.json", "sha256": "x"}}
        path = tmp_path / "index.json"
        path.write_text(json.dumps(index))
        with pytest.raises(yostar.MediaError):
            yostar.load_media(str(path), game_tag="X")

    def test_tag_mismatch_raises(self, tmp_path):
        path = tmp_path / "media.json"
        path.write_text(json.dumps(_media()))
        with pytest.raises(yostar.MediaError):
            yostar.load_media(str(path), game_tag="StellaSora_EN")

    def test_rejects_bad_shape(self, tmp_path):
        for payload in ([], {}, {"game_tag": "X", "banners": "no"}):
            path = tmp_path / "m.json"
            path.write_text(json.dumps(payload))
            with pytest.raises(yostar.MediaError):
                yostar.load_media(str(path))

    def test_configured_media_env_and_profile(self, monkeypatch):
        assert yostar.configured_media({}, {"YOSTAR_MEDIA_FILE": "https://x/m.json"}) == (
            "https://x/m.json"
        )
        assert yostar.configured_media({"media_file": "/tmp/m.json"}, {}) == "/tmp/m.json"
        assert yostar.configured_media({"extra": {"media_file": "/tmp/e.json"}}, {}) == (
            "/tmp/e.json"
        )
        assert yostar.configured_media({}, {}) == ""


class TestMediaDownloads:
    def test_only_yo_star_https_is_allowed(self):
        assert yostar.media_url_allowed("https://launcher-pkg-ark-en.yo-star.com/a.jpg")
        assert not yostar.media_url_allowed("http://launcher-pkg-ark-en.yo-star.com/a.jpg")
        assert not yostar.media_url_allowed("https://evil.example/a.jpg")
        assert not yostar.media_url_allowed("https://launcher-pkg-ark-en.yo-star.com:8443/a.jpg")
        assert not yostar.media_url_allowed("https://u:p@launcher-pkg-ark-en.yo-star.com/a.jpg")

    def test_cache_media_downloads_hero_and_banners(self, monkeypatch):
        from nadare.core import artwork

        calls: list[tuple[str, str, str, tuple]] = []

        def fake_download(url, slug, kind, hosts=()):
            calls.append((url, slug, kind, tuple(hosts)))
            return Path(f"/cache/{slug}-{kind}.jpg")

        monkeypatch.setattr(artwork, "download", fake_download)
        media = yostar.Media(
            game_tag="Arknights_EN",
            generated_at="t",
            background="https://launcher-pkg-ark-en.yo-star.com/bg.jpg",
            banners=(
                yostar.Banner("https://launcher-pkg-ark-en.yo-star.com/b1.jpg", "https://n/1"),
                yostar.Banner("https://evil.example/b2.jpg", "https://n/2"),
            ),
        )
        result = yostar.cache_media(media, "arknights")
        assert result["hero"] == "/cache/arknights-official-hero.jpg"
        assert result["banners"] == [
            {"image_path": "/cache/arknights-official-banner-0.jpg", "url": "https://n/1"}
        ]
        assert [c[2] for c in calls] == ["official-hero", "official-banner-0"]
        # M1 residual: the validated publisher host is the official trust anchor.
        assert [c[3] for c in calls] == [
            ("launcher-pkg-ark-en.yo-star.com",),
            ("launcher-pkg-ark-en.yo-star.com",),
        ]

    def test_media_host_scopes_the_allowlist_to_the_url_host(self):
        assert yostar._media_host("https://a.yo-star.com/x.jpg") == ("a.yo-star.com",)
        assert yostar._media_host("") == ()


# ---------------------------------------------------------------------------
# loopback trust is a local-test affordance only (FRESH-5)
# ---------------------------------------------------------------------------
class TestLoopbackManifestSource:
    def _man(self, source: str) -> yostar.Manifest:
        return yostar.Manifest(
            game_tag="t",
            version="1",
            base_url="http://127.0.0.1:8123/assets",
            generated_at="",
            source=source,
            files=(yostar.ManifestFile("x.bin", 1, yostar.crc64(b"x")),),
        )

    def test_source_is_local_classifies_sources(self):
        assert yostar._source_is_local("")
        assert yostar._source_is_local("/tmp/manifest.json")
        assert yostar._source_is_local("http://localhost:8000/index.json")
        assert not yostar._source_is_local("https://cdn.example/index.json")

    def test_remote_manifest_cannot_redirect_downloads_to_loopback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            yostar.download,
            "stream_download",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")),
        )
        with pytest.raises(yostar.ManifestError, match="loopback"):
            yostar.sync(self._man("https://index.example/czn/index.json"), tmp_path)

    def test_local_manifest_source_may_use_loopback(self, tmp_path, monkeypatch):
        payload = b"x"
        man = self._man("")

        def fake_stream(url, dest, **kwargs):
            part = Path(dest).with_name(Path(dest).name + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(payload)
            hasher = kwargs.get("hasher")
            if hasher is not None:
                hasher.reset()
                hasher.update(payload)
            on_progress = kwargs.get("on_progress")
            if on_progress is not None:
                on_progress(len(payload), len(payload), len(payload))
            return download_mod.StreamResult(Path(dest), len(payload))

        monkeypatch.setattr(yostar.download, "stream_download", fake_stream)
        yostar.sync(man, tmp_path, workers=1)
        assert (tmp_path / "x.bin").read_bytes() == payload


def test_safe_relative_path_rejects_nul() -> None:
    with pytest.raises(yostar.ManifestError):
        yostar.safe_relative_path("a\x00b")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.jpg",
        "https://127.5.5.5/a.jpg",
        "https://localhost/a.jpg",
        "https://[::1]/a.jpg",
        "https://10.1.2.3/a.jpg",
        "https://172.16.0.1/a.jpg",
        "https://192.168.1.1/a.jpg",
        "https://169.254.1.1/a.jpg",
    ],
)
def test_media_url_rejects_local_and_private(url: str) -> None:
    assert not yostar.media_url_allowed(url)


def test_media_url_still_allows_yostar() -> None:
    assert yostar.media_url_allowed("https://cdn.yo-star.com/a.jpg")


def test_self_referential_index_raises_manifest_error(tmp_path: Path) -> None:
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"tag": {"url": "index.json"}}))
    with pytest.raises(yostar.ManifestError):
        yostar.load_manifest(str(idx), game_tag="tag")


# ---------------------------------------------------------------------------
# duplicate paths would race on one .part file (#26)
# ---------------------------------------------------------------------------
def _manifest_with(paths):
    return {
        "game_tag": "T",
        "version": "1",
        "base_url": "https://cdn.example/x",
        "files": [{"path": p, "size": 1, "crc64": 0} for p in paths],
    }


@pytest.mark.parametrize(
    "paths",
    [
        ["a/b.ab", "a/b.ab"],
        ["a/b.ab", "a//b.ab"],  # same file after normalisation
        ["A/B.ab", "a/b.ab"],  # Windows/Wine resolve case-insensitively
    ],
)
def test_manifest_rejects_duplicate_paths(paths):
    with pytest.raises(yostar.ManifestError, match="duplicate"):
        yostar._parse_manifest(_manifest_with(paths))


def test_manifest_accepts_distinct_paths():
    m = yostar._parse_manifest(_manifest_with(["a/b.ab", "a/c.ab", "d/b.ab"]))
    assert len(m.files) == 3
