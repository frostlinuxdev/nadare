# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the CI Yostar manifest generator.

The generator is a standalone script under ``ci/yostar-manifests``; it is
loaded by path and its HTTP layer is replaced with a fake so nothing touches the
network. The only salt used is ``"test-salt"``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nadare.core import yostar

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "ci" / "yostar-manifests" / "generate.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("yostar_generate", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["yostar_generate"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gen():
    return _load_generator()


class FakeHttp:
    """Maps URLs to JSON payloads and records the request headers."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        self.requests.append((url, dict(headers or {})))
        if url not in self.routes:
            raise AssertionError(f"unexpected URL: {url}")
        payload = self.routes[url]
        if isinstance(payload, bytes):
            return payload
        return json.dumps(payload).encode()

    def auth_headers(self) -> list[dict[str, str]]:
        return [h for _u, h in self.requests if "Authorization" in h]


def _payloads(gen):
    cfg_url = gen.API_BASE + "/api/launcher/game/config"
    info_url = gen.API_BASE + "/api/launcher/game/config/json?version=7&file_path=pc/manifest.json"
    raw_url = "https://cdn.yo-star.com/pc/7/manifest.json"
    raw = {
        "source": "game/asset",
        "file": [
            {"path": "/StellaSora.exe", "hash": str(yostar.crc64(b"exe")), "size": "3"},
            {"path": "data/a.bin", "hash": "0x995dc9bbdf1939fa", "size": 9},
        ],
    }
    return cfg_url, info_url, raw_url, raw


# ---------------------------------------------------------------------------
# the three-step flow
# ---------------------------------------------------------------------------
class TestGenerate:
    def test_three_step_flow_and_output_shape(self, gen, monkeypatch):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {"game_latest_version": 7, "game_latest_file_path": "pc/manifest.json"}
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        result = gen.generate_game("StellaSora_EN", "1.2.3", "test-salt")

        assert result["game_tag"] == "StellaSora_EN"
        assert result["version"] == "7"
        assert result["base_url"] == "https://cdn.yo-star.com/game/asset"
        assert result["files"][0]["path"] == "StellaSora.exe"
        assert result["files"][0]["size"] == 3
        assert result["files"][0]["crc64"] == yostar.crc64(b"exe")
        assert result["files"][1]["crc64"] == 0x995DC9BBDF1939FA
        # Step 1 and 2 were signed; step 3 (public CDN) was not.
        auth = fake.auth_headers()
        assert len(auth) == 2
        for headers in auth:
            assert headers["Accept"] == "application/json, text/plain, */*"
            assert "test-salt" not in headers["Authorization"]
        assert fake.requests[-1][1].get("Authorization") is None

    def test_signature_binds_the_head(self, gen, monkeypatch):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {
                        "game_latest_version": 7,
                        "game_latest_file_path": "pc/manifest.json",
                    }
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        gen.generate_game("StellaSora_EN", "1.2.3", "test-salt")
        head = gen._parse_authorization(fake.auth_headers()[0]["Authorization"])
        assert head["head"]["game_tag"] == "StellaSora_EN"
        assert head["head"]["version"] == "1.2.3"
        assert isinstance(head["head"]["time"], int)

    def test_error_envelope_surfaces_code_and_msg(self, gen, monkeypatch):
        cfg_url = gen.API_BASE + "/api/launcher/game/config"
        fake = FakeHttp({cfg_url: {"code": 403, "msg": "signature mismatch"}})
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        with pytest.raises(gen.YostarApiError) as excinfo:
            gen.generate_game("StellaSora_EN", "1.2.3", "test-salt")
        assert excinfo.value.code == 403
        assert "signature mismatch" in str(excinfo.value)

    def test_hex_and_decimal_hashes_normalise(self, gen, monkeypatch):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {
                        "game_latest_version": 7,
                        "game_latest_file_path": "pc/manifest.json",
                    }
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        result = gen.generate_game("StellaSora_EN", "1", "test-salt")
        assert all(isinstance(f["crc64"], int) for f in result["files"])


# ---------------------------------------------------------------------------
# writing outputs
# ---------------------------------------------------------------------------
class TestOutputs:
    def test_writes_manifests_and_index(self, gen, tmp_path, monkeypatch):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {
                        "game_latest_version": 7,
                        "game_latest_file_path": "pc/manifest.json",
                    }
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        manifest = gen.generate_game("StellaSora_EN", "1.2.3", "test-salt")
        index = gen.write_outputs([manifest], tmp_path)

        leaf = tmp_path / "StellaSora_EN.json"
        assert leaf.is_file()
        assert json.loads(leaf.read_text())["game_tag"] == "StellaSora_EN"
        assert index["StellaSora_EN"]["version"] == "7"
        assert index["StellaSora_EN"]["url"] == "StellaSora_EN.json"
        import hashlib

        assert index["StellaSora_EN"]["sha256"] == hashlib.sha256(leaf.read_bytes()).hexdigest()
        assert json.loads((tmp_path / "index.json").read_text()) == index

    def test_salt_never_in_outputs_or_stdout(self, gen, tmp_path, monkeypatch, capsys):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {
                        "game_latest_version": 7,
                        "game_latest_file_path": "pc/manifest.json",
                    }
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        monkeypatch.setenv("SALT_STELLASORA_EN", "test-salt")
        monkeypatch.setenv("VERSION_STELLASORA_EN", "1.2.3")
        gen.main(["--out", str(tmp_path)])
        captured = capsys.readouterr()
        blob = "\n".join(p.read_text() for p in tmp_path.glob("*.json"))
        for text in (captured.out, captured.err, blob):
            assert "test-salt" not in text

    def test_skips_game_without_salt(self, gen, tmp_path, monkeypatch, capsys):
        called = []

        def boom(*a, **k):
            called.append(1)
            raise AssertionError("must not fetch without a salt")

        monkeypatch.setattr(gen, "_http_get", boom, raising=False)
        monkeypatch.delenv("SALT_STELLASORA_EN", raising=False)
        monkeypatch.setenv("SALT_ARKNIGHTS_EN", "test-salt")
        # Only StellaSora has a salt configured version but no salt; nothing runs.
        monkeypatch.setenv("VERSION_STELLASORA_EN", "1")
        monkeypatch.delenv("VERSION_ARKNIGHTS_EN", raising=False)
        gen.main(["--out", str(tmp_path)])
        captured = capsys.readouterr()
        assert "StellaSora_EN" in captured.err
        assert called == []


def test_parser_reads_index_roundtrip(gen):
    header = yostar.authorization_header("t", "v", "test-salt", time=10)
    parsed = gen._parse_authorization(header)
    assert parsed["head"]["time"] == 10


def test_http_get_sends_a_non_default_user_agent(gen, monkeypatch) -> None:
    """Regression: Yostar's CDN answers 403 to Python's default ``Python-urllib`` UA."""
    seen: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout=None):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return _Resp()

    monkeypatch.setattr(gen.urllib.request, "urlopen", fake_urlopen)
    gen._http_get("https://launcher-pkg-ss-en.yo-star.com/x.json")
    assert "user-agent" in seen
    assert not seen["user-agent"].startswith("Python-urllib")


# ---------------------------------------------------------------------------
# official media
# ---------------------------------------------------------------------------
class MediaFakeHttp(FakeHttp):
    """Fake HTTP with the media endpoints wired to canned payloads."""

    def __init__(self, base, operations, extra):
        routes = {
            gen_api("/api/launcher/base/config"): base,
            gen_api("/api/launcher/operations/resource"): operations,
        }
        routes.update(extra)
        super().__init__(routes)


def gen_api(path: str) -> str:
    return "https://api-launcher-en.yo-star.com" + path


def _media_http():
    base = {
        "data": {
            "launcher_background_img": (
                "https://launcher-pkg-ark-en.yo-star.com/img/background.jpg"
            )
        }
    }
    operations = {
        "data": {
            "operations_banner_list": [
                {
                    "banner_img": "https://launcher-pkg-ark-en.yo-star.com/img/b1.png",
                    "jump_url": "https://www.arknights.global/news/1",
                    "remark": "Event",
                    "start_at": 1,
                    "end_at": 2,
                },
                {"banner_img": ""},
            ],
            "news_list": {"any": "ignored"},
        }
    }
    return MediaFakeHttp(base, operations, {})


class TestGenerateMedia:
    def test_builds_background_and_banners(self, gen, monkeypatch):
        fake = _media_http()
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        media = gen.generate_media("Arknights_EN", "1.2.3", "test-salt", generated_at="now")

        assert media["game_tag"] == "Arknights_EN"
        assert media["generated_at"] == "now"
        assert media["background"].endswith("background.jpg")
        assert media["banners"] == [
            {
                "image": "https://launcher-pkg-ark-en.yo-star.com/img/b1.png",
                "url": "https://www.arknights.global/news/1",
            }
        ]
        # Both calls were signed, and the salt never leaks into the payload.
        assert len(fake.auth_headers()) == 2
        assert "test-salt" not in json.dumps(media)

    def test_missing_fields_degrade_to_empty(self, gen):
        media = gen.build_media("X", {}, {}, generated_at="t")
        assert media["background"] is None
        assert media["banners"] == []

    def test_write_outputs_publishes_media_and_index_entry(self, gen, tmp_path):
        manifest = {
            "game_tag": "Arknights_EN",
            "version": "7",
            "base_url": "https://cdn.yo-star.com/a",
            "generated_at": "t",
            "files": [],
        }
        media = {
            "Arknights_EN": {
                "game_tag": "Arknights_EN",
                "generated_at": "t",
                "background": "https://launcher-pkg-ark-en.yo-star.com/bg.jpg",
                "banners": [],
            }
        }
        index = gen.write_outputs([manifest], tmp_path, media=media)
        leaf = tmp_path / "Arknights_EN.media.json"
        assert leaf.is_file()
        assert index["Arknights_EN"]["media"] == "Arknights_EN.media.json"
        assert json.loads(leaf.read_text())["background"] == media["Arknights_EN"]["background"]
        assert json.loads((tmp_path / "index.json").read_text()) == index

    def test_media_failure_never_fails_the_manifest(self, gen, tmp_path, monkeypatch, capsys):
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        fake = FakeHttp(
            {
                cfg_url: {
                    "data": {
                        "game_latest_version": 7,
                        "game_latest_file_path": "pc/manifest.json",
                    }
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", fake, raising=False)
        monkeypatch.setenv("SALT_STELLASORA_EN", "test-salt")
        monkeypatch.setenv("VERSION_STELLASORA_EN", "1.2.3")
        monkeypatch.delenv("SALT_ARKNIGHTS_EN", raising=False)
        gen.main(["--out", str(tmp_path)])
        assert (tmp_path / "StellaSora_EN.json").is_file()
        assert not (tmp_path / "StellaSora_EN.media.json").exists()
        assert "media" not in json.loads((tmp_path / "index.json").read_text())["StellaSora_EN"]
        captured = capsys.readouterr()
        assert "test-salt" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# merge / --merge-into (keep-last-good, fail-closed)
# ---------------------------------------------------------------------------
def _manifest(tag: str, version: str = "1") -> dict:
    return {
        "game_tag": tag,
        "version": version,
        "base_url": "https://cdn.yo-star.com/game/asset",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "files": [{"path": f"{tag}.exe", "size": 1, "crc64": 1}],
    }


def _stub_generate(gen, monkeypatch, func) -> None:
    """Replace ``generate_game`` with ``func(tag, version)`` (may raise)."""
    monkeypatch.setattr(
        gen,
        "generate_game",
        lambda tag, version, salt, *, generated_at=None: func(tag, version),
    )


class TestMergeInto:
    TAGS = {"GameA_EN": "a", "GameB_EN": "b"}

    @pytest.fixture()
    def wired(self, gen, monkeypatch):
        monkeypatch.setattr(gen, "GAME_TAGS", dict(self.TAGS))
        for tag in self.TAGS:
            monkeypatch.setenv(gen._env_key("SALT_", tag), "test-salt")
            monkeypatch.setenv(gen._env_key("VERSION_", tag), "1")
        monkeypatch.setattr(gen, "generate_media", lambda *a, **k: {})
        return gen

    def test_bootstrap_merge_publishes_every_tag(self, wired, monkeypatch, tmp_path):
        gen = wired
        _stub_generate(gen, monkeypatch, _manifest)
        pub = tmp_path / "published"
        assert gen.main(["--out", str(tmp_path / "fresh"), "--merge-into", str(pub)]) == 0
        index = json.loads((pub / "index.json").read_text())
        assert set(index) == set(self.TAGS)
        for tag in self.TAGS:
            assert (pub / f"{tag}.json").is_file()

    def test_flaky_tag_keeps_last_good(self, wired, monkeypatch, tmp_path):
        gen = wired
        pub = tmp_path / "published"
        _stub_generate(gen, monkeypatch, _manifest)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        good_b = (pub / "GameB_EN.json").read_bytes()
        entry_b = json.loads((pub / "index.json").read_text())["GameB_EN"]

        def flaky(tag, version):
            if tag == "GameB_EN":
                raise gen.YostarApiError(None, "upstream down")
            return _manifest(tag, "2")

        _stub_generate(gen, monkeypatch, flaky)
        assert gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)]) == 0
        # The healthy tag updated; the flaky tag kept its last-good file/entry.
        assert (pub / "GameB_EN.json").read_bytes() == good_b
        assert json.loads((pub / "index.json").read_text())["GameB_EN"] == entry_b
        assert json.loads((pub / "index.json").read_text())["GameA_EN"]["version"] == "2"

    def test_all_skipped_exits_nonzero_without_mutating(self, wired, monkeypatch, tmp_path):
        gen = wired
        pub = tmp_path / "published"
        _stub_generate(gen, monkeypatch, _manifest)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        before = {p.name: p.read_bytes() for p in pub.glob("*.json")}

        def boom(tag, version):
            raise gen.YostarApiError(None, "down")

        _stub_generate(gen, monkeypatch, boom)
        # R4-4: "nothing to generate" is its own exit code (2), not the error 1.
        assert gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)]) == (
            gen.NOTHING_TO_GENERATE
        )
        after = {p.name: p.read_bytes() for p in pub.glob("*.json")}
        assert after == before

    def test_merge_dropping_a_published_tag_fails_closed(self, wired, monkeypatch, tmp_path):
        """R4-2: if a merge ever drops a kept tag, exit non-zero (invariant)."""
        gen = wired
        pub = tmp_path / "published"
        _stub_generate(gen, monkeypatch, _manifest)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0

        def dropping(fresh_dir, published_dir):
            # Simulate a future merge_outputs regression that drops GameA.
            index = json.loads((Path(published_dir) / "index.json").read_text())
            index.pop("GameA_EN", None)
            return index, [], []

        monkeypatch.setattr(gen, "merge_outputs", dropping)
        assert gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)]) == 1

    def test_empty_payload_never_overwrites_good(self, wired, monkeypatch, tmp_path):
        gen = wired
        pub = tmp_path / "published"
        _stub_generate(gen, monkeypatch, _manifest)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        good_b = (pub / "GameB_EN.json").read_bytes()
        entry_b = json.loads((pub / "index.json").read_text())["GameB_EN"]

        def empty(tag, version):
            if tag == "GameB_EN":
                raise gen.YostarApiError(None, "manifest lists no files")
            return _manifest(tag, "2")

        _stub_generate(gen, monkeypatch, empty)
        assert gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)]) == 0
        assert (pub / "GameB_EN.json").read_bytes() == good_b
        assert json.loads((pub / "index.json").read_text())["GameB_EN"] == entry_b

    def test_new_failing_tag_does_not_block_healthy_publish(
        self, wired, monkeypatch, tmp_path, capsys
    ):
        gen = wired
        pub = tmp_path / "published"
        # Bootstrap only GameA.
        monkeypatch.delenv("SALT_GAMEB_EN", raising=False)
        monkeypatch.delenv("VERSION_GAMEB_EN", raising=False)
        _stub_generate(gen, monkeypatch, _manifest)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        # Add GameB; its first generation fails, GameA must still publish.
        monkeypatch.setenv("SALT_GAMEB_EN", "test-salt")
        monkeypatch.setenv("VERSION_GAMEB_EN", "1")

        def half(tag, version):
            if tag == "GameB_EN":
                raise gen.YostarApiError(None, "not live yet")
            return _manifest(tag, "2")

        _stub_generate(gen, monkeypatch, half)
        rc = gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)])
        assert rc == 0
        index = json.loads((pub / "index.json").read_text())
        assert index["GameA_EN"]["version"] == "2"
        assert "GameB_EN" not in index
        assert "not yet published" in capsys.readouterr().err

    def test_salt_without_version_is_not_configured(self, wired, monkeypatch, tmp_path, capsys):
        gen = wired
        pub = tmp_path / "published"
        _stub_generate(gen, monkeypatch, _manifest)
        # Publish GameA only, so the index already exists.
        monkeypatch.delenv("SALT_GAMEB_EN", raising=False)
        monkeypatch.delenv("VERSION_GAMEB_EN", raising=False)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        # A salt with no version is a half-rolled-out game: skipped, not failed.
        monkeypatch.setenv("SALT_GAMEB_EN", "test-salt")
        monkeypatch.delenv("VERSION_GAMEB_EN", raising=False)
        capsys.readouterr()
        rc = gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)])
        err = capsys.readouterr().err
        assert rc == 0
        assert "GameB_EN" in err
        assert "not yet published" not in err
        assert "GameB_EN" not in json.loads((pub / "index.json").read_text())

    def test_empty_payload_end_to_end_keeps_published(self, gen, monkeypatch, tmp_path):
        """A valid-JSON but empty upstream payload must not replace a good file."""
        cfg_url, info_url, raw_url, raw = _payloads(gen)
        monkeypatch.setattr(gen, "GAME_TAGS", {"StellaSora_EN": "stellasora-yostar"})
        monkeypatch.setenv("SALT_STELLASORA_EN", "test-salt")
        monkeypatch.setenv("VERSION_STELLASORA_EN", "1.2.3")
        monkeypatch.setattr(gen, "generate_media", lambda *a, **k: {})
        pub = tmp_path / "published"
        good = FakeHttp(
            {
                cfg_url: {
                    "data": {"game_latest_version": 7, "game_latest_file_path": "pc/manifest.json"}
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: raw,
            }
        )
        monkeypatch.setattr(gen, "_http_get", good, raising=False)
        assert gen.main(["--out", str(tmp_path / "f1"), "--merge-into", str(pub)]) == 0
        good_bytes = (pub / "StellaSora_EN.json").read_bytes()
        index_bytes = (pub / "index.json").read_bytes()

        empty = FakeHttp(
            {
                cfg_url: {
                    "data": {"game_latest_version": 7, "game_latest_file_path": "pc/manifest.json"}
                },
                info_url: {"data": {"url": raw_url}},
                raw_url: {"source": "game/asset", "file": []},
            }
        )
        monkeypatch.setattr(gen, "_http_get", empty, raising=False)
        assert gen.main(["--out", str(tmp_path / "f2"), "--merge-into", str(pub)]) == (
            gen.NOTHING_TO_GENERATE
        )
        assert (pub / "StellaSora_EN.json").read_bytes() == good_bytes
        assert (pub / "index.json").read_bytes() == index_bytes


def test_build_manifest_rejects_empty_file_list(gen):
    url = "https://cdn.yo-star.com/pc/manifest.json"
    with pytest.raises(gen.YostarApiError, match="no files"):
        gen.build_manifest("GameA_EN", "1", {"source": "s", "file": []}, url)
    # A missing ``file`` key is the same failure.
    with pytest.raises(gen.YostarApiError, match="no files"):
        gen.build_manifest("GameA_EN", "1", {"source": "s"}, url)
    # R4-1: a ``null`` file list is an empty payload, not a ``TypeError`` crash.
    with pytest.raises(gen.YostarApiError, match="no files"):
        gen.build_manifest("GameA_EN", "1", {"source": "s", "file": None}, url)
    # Any other non-list container is malformed, not iterated.
    with pytest.raises(gen.YostarApiError, match="not a list"):
        gen.build_manifest("GameA_EN", "1", {"source": "s", "file": 7}, url)
    # A non-dict payload is rejected too.
    with pytest.raises(gen.YostarApiError, match="malformed"):
        gen.build_manifest("GameA_EN", "1", ["nope"], url)


def test_null_file_payload_skips_the_tag(gen, monkeypatch, tmp_path, capsys):
    """R4-1: ``{"file": null}`` must skip the tag, not abort the whole run."""
    cfg_url, info_url, raw_url, _ = _payloads(gen)
    monkeypatch.setattr(gen, "GAME_TAGS", {"StellaSora_EN": "stellasora-yostar"})
    monkeypatch.setenv("SALT_STELLASORA_EN", "test-salt")
    monkeypatch.setenv("VERSION_STELLASORA_EN", "1.2.3")
    monkeypatch.setattr(gen, "generate_media", lambda *a, **k: {})
    fake = FakeHttp(
        {
            cfg_url: {
                "data": {"game_latest_version": 7, "game_latest_file_path": "pc/manifest.json"}
            },
            info_url: {"data": {"url": raw_url}},
            raw_url: {"source": "game/asset", "file": None},
        }
    )
    monkeypatch.setattr(gen, "_http_get", fake, raising=False)
    assert gen.main(["--out", str(tmp_path)]) == gen.NOTHING_TO_GENERATE
    assert "StellaSora_EN" in capsys.readouterr().err


def _publish_index_module():
    spec = importlib.util.spec_from_file_location(
        "publish_index", _REPO_ROOT / "scripts" / "publish_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stub_generator_exit(monkeypatch, module, returncode):
    """Make the generator subprocess exit ``returncode`` without touching the
    shared ``subprocess`` module (R4-6)."""
    import subprocess
    import types

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode, args[0] if args else "generate.py")

    monkeypatch.setattr(
        module,
        "subprocess",
        types.SimpleNamespace(run=boom, CalledProcessError=subprocess.CalledProcessError),
    )


def test_publish_index_keeps_last_good_on_nothing_to_publish(monkeypatch, tmp_path):
    """R4-4: the dedicated "nothing to generate" exit keeps last-good, no traceback."""
    module = _publish_index_module()
    _stub_generator_exit(monkeypatch, module, module.NOTHING_TO_GENERATE)
    out = tmp_path / "yostar"
    out.mkdir()
    (out / "index.json").write_text(
        '{"GameA_EN": {"version": "1", "url": "GameA_EN.json", "sha256": "x"}}\n'
    )
    (out / "GameA_EN.json").write_text('{"files": [{"path": "a"}]}\n')
    before = {p.name: p.read_bytes() for p in out.glob("*.json")}
    assert module.publish_yostar(out, {}) is False
    after = {p.name: p.read_bytes() for p in out.glob("*.json")}
    assert after == before


def test_publish_index_surfaces_a_generator_crash(monkeypatch, tmp_path):
    """R4-4: a real generator failure must not be swallowed as "nothing to publish"."""
    module = _publish_index_module()
    _stub_generator_exit(monkeypatch, module, 1)
    out = tmp_path / "yostar"
    out.mkdir()
    with pytest.raises(RuntimeError, match="generator failed"):
        module.publish_yostar(out, {})


def _stub_probe_result(monkeypatch, module, stdout: str):
    """Make the CZN probe succeed with ``stdout`` as its captured output."""

    def fake_run(*args, **kwargs):
        import types

        return types.SimpleNamespace(stdout=stdout)

    import subprocess
    import types

    monkeypatch.setattr(
        module,
        "subprocess",
        types.SimpleNamespace(run=fake_run, CalledProcessError=subprocess.CalledProcessError),
    )


def test_publish_czn_writes_a_probe_result_then_is_quiet_when_unchanged(monkeypatch, tmp_path):
    """07-NEW-5: a successful probe publishes ``latest.json``, then is a no-op."""
    module = _publish_index_module()
    _stub_probe_result(
        monkeypatch,
        module,
        json.dumps({"service": "STOVE_CHAOSZERO", "latest": 60, "checked_at": 1}),
    )
    out = tmp_path / "czn"
    assert module.publish_czn(out, {}) is True
    assert json.loads((out / "latest.json").read_text(encoding="utf-8"))["latest"] == 60
    # The same payload on a second run differs only by timestamp -> no publish.
    assert module.publish_czn(out, {}) is False


def test_publish_czn_keeps_last_good_on_nothing_to_publish(monkeypatch, tmp_path):
    """07-NEW-5: the probe's "nothing published" exit keeps last-good, no traceback."""
    module = _publish_index_module()
    _stub_generator_exit(monkeypatch, module, module.NOTHING_TO_GENERATE)
    out = tmp_path / "czn"
    out.mkdir()
    (out / "latest.json").write_text(
        '{"service": "STOVE_CHAOSZERO", "latest": 55, "checked_at": 1}\n'
    )
    before = (out / "latest.json").read_bytes()
    assert module.publish_czn(out, {}) is False
    assert (out / "latest.json").read_bytes() == before


def test_publish_czn_surfaces_a_probe_crash(monkeypatch, tmp_path):
    """07-NEW-5: a real probe failure must not be swallowed as "nothing to publish"."""
    module = _publish_index_module()
    _stub_generator_exit(monkeypatch, module, 1)
    out = tmp_path / "czn"
    out.mkdir()
    with pytest.raises(RuntimeError, match="probe failed"):
        module.publish_czn(out, {})


class _FakeResp:
    def __init__(self, body: bytes, *, size: int | None = None, url: str, modified: str = "M1"):
        import io

        self._buf = io.BytesIO(body)
        self.headers = {
            "Content-Length": str(len(body) if size is None else size),
            "Last-Modified": modified,
        }
        self._url = url

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _webview2_opener(module, body: bytes, *, url=None, modified="M1", calls=None):
    url = url or "https://msedge.sf.dl.delivery.mp.microsoft.com/x/Installer.exe"

    def opener(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "method", None) or "GET")
        return _FakeResp(body, url=url, modified=modified)

    return opener


def test_publish_webview2_records_hash_and_keeps_history(tmp_path):
    import hashlib

    module = _publish_index_module()
    out = tmp_path / "webview2"
    body1 = b"A" * 50_000_000
    assert module.publish_webview2(out, {}, _webview2_opener(module, body1)) is True
    data = json.loads((out / "latest.json").read_text())
    assert data["sha256"] == [hashlib.sha256(body1).hexdigest()]
    body2 = b"B" * 50_000_001
    assert module.publish_webview2(out, {}, _webview2_opener(module, body2, modified="M2")) is True
    data = json.loads((out / "latest.json").read_text())
    assert data["sha256"] == [
        hashlib.sha256(body2).hexdigest(),
        hashlib.sha256(body1).hexdigest(),
    ]


def test_publish_webview2_skips_download_when_unchanged(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "webview2"
    body = b"A" * 50_000_000
    assert module.publish_webview2(out, {}, _webview2_opener(module, body)) is True
    calls: list[str] = []
    assert module.publish_webview2(out, {}, _webview2_opener(module, body, calls=calls)) is False
    assert calls == ["HEAD"]


def test_publish_webview2_keeps_last_good_off_microsoft_or_short(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "webview2"
    body = b"A" * 50_000_000
    assert module.publish_webview2(out, {}, _webview2_opener(module, body)) is True
    before = (out / "latest.json").read_bytes()
    evil = _webview2_opener(
        module, b"B" * 50_000_001, url="https://evil.example/x.exe", modified="M2"
    )
    assert module.publish_webview2(out, {}, evil) is False
    short = _webview2_opener(module, b"tiny", modified="M3")
    assert module.publish_webview2(out, {}, short) is False
    assert (out / "latest.json").read_bytes() == before


def _vcredist_opener(module, body: bytes, *, url=None, modified="M1", calls=None):
    url = url or "https://download.visualstudio.microsoft.com/x/vc_redist.x64.exe"

    def opener(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "method", None) or "GET")
        return _FakeResp(body, url=url, modified=modified)

    return opener


def test_publish_vcredist_records_hash_and_keeps_history(tmp_path):
    import hashlib

    module = _publish_index_module()
    out = tmp_path / "vcredist"
    body1 = b"A" * 10_000_000
    assert module.publish_vcredist(out, {}, _vcredist_opener(module, body1)) is True
    data = json.loads((out / "latest.json").read_text())
    assert data["sha256"] == [hashlib.sha256(body1).hexdigest()]
    assert data["url"].endswith("vc_redist.x64.exe")
    body2 = b"B" * 10_000_001
    assert module.publish_vcredist(out, {}, _vcredist_opener(module, body2, modified="M2")) is True
    data = json.loads((out / "latest.json").read_text())
    assert data["sha256"] == [
        hashlib.sha256(body2).hexdigest(),
        hashlib.sha256(body1).hexdigest(),
    ]


def test_publish_vcredist_skips_download_when_unchanged(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    body = b"A" * 10_000_000
    assert module.publish_vcredist(out, {}, _vcredist_opener(module, body)) is True
    calls: list[str] = []
    assert module.publish_vcredist(out, {}, _vcredist_opener(module, body, calls=calls)) is False
    assert calls == ["HEAD"]


def test_publish_vcredist_keeps_last_good_off_microsoft_or_short(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    body = b"A" * 10_000_000
    assert module.publish_vcredist(out, {}, _vcredist_opener(module, body)) is True
    before = (out / "latest.json").read_bytes()
    evil = _vcredist_opener(
        module, b"B" * 10_000_001, url="https://evil.example/x.exe", modified="M2"
    )
    assert module.publish_vcredist(out, {}, evil) is False
    short = _vcredist_opener(module, b"tiny", modified="M3")
    assert module.publish_vcredist(out, {}, short) is False
    assert (out / "latest.json").read_bytes() == before


def test_publish_vcredist_accepts_aka_ms_and_rejects_non_https(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    body = b"A" * 10_000_000
    aka = _vcredist_opener(module, body, url="https://aka.ms/vs/17/release/vc_redist.x64.exe")
    assert module.publish_vcredist(out, {}, aka) is True
    before = (out / "latest.json").read_bytes()
    insecure = _vcredist_opener(
        module,
        b"B" * 10_000_001,
        url="http://download.visualstudio.microsoft.com/x/vc_redist.x64.exe",
        modified="M2",
    )
    assert module.publish_vcredist(out, {}, insecure) is False
    assert (out / "latest.json").read_bytes() == before


# ---------------------------------------------------------------------------
# index files are written atomically; a corrupt one is never "reset" (#79)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("loader", ["publish_index", "generator"])
def test_atomic_write_leaves_the_old_file_when_the_replace_fails(monkeypatch, tmp_path, loader):
    module = _publish_index_module() if loader == "publish_index" else _load_generator()
    dest = tmp_path / "pub" / "index.json"
    dest.parent.mkdir()
    dest.write_bytes(b'{"old": true}\n')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "replace", boom)
    with pytest.raises(OSError):
        module._atomic_write_bytes(dest, b'{"new": true}\n')
    assert dest.read_bytes() == b'{"old": true}\n'
    assert [p.name for p in dest.parent.iterdir()] == ["index.json"]  # no temp left behind


@pytest.mark.parametrize("loader", ["publish_index", "generator"])
def test_atomic_write_replaces_in_one_step(tmp_path, loader):
    module = _publish_index_module() if loader == "publish_index" else _load_generator()
    dest = tmp_path / "sub" / "index.json"
    module._atomic_write_bytes(dest, b"{}\n")
    assert dest.read_bytes() == b"{}\n"
    assert [p.name for p in dest.parent.iterdir()] == ["index.json"]


def test_corrupt_webview2_history_is_refused_not_reset(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "webview2"
    out.mkdir()
    (out / "latest.json").write_text('{"sha256": ["abc"')  # truncated write
    with pytest.raises(RuntimeError, match="latest.json"):
        module.publish_webview2(out, {}, _webview2_opener(module, b"A" * 50_000_000))
    assert (out / "latest.json").read_text() == '{"sha256": ["abc"'  # untouched


def test_corrupt_vcredist_history_is_refused_not_reset(tmp_path):
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    out.mkdir()
    (out / "latest.json").write_text("not json")
    with pytest.raises(RuntimeError, match="latest.json"):
        module.publish_vcredist(out, {}, _webview2_opener(module, b"A" * 50_000_000))
    assert (out / "latest.json").read_text() == "not json"
