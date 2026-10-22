# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the network/download hardening sweep.

Everything is hermetic: HTTP clients, resolvers and subprocesses are faked.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

from nadare.backends.fgo import lepton
from nadare.core import artwork, download, updatecheck, vcredist
from nadare.core.events import Cancel

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. vcredist: concurrent installs must not share one .part path
# ---------------------------------------------------------------------------
def test_vcredist_uses_a_unique_part_path_per_download(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    seen: list[Path] = []

    def download_fn(_url: str, tmp: Path, _dest: Path) -> None:
        seen.append(tmp)
        raise RuntimeError("stop after recording the temp path")

    def run_install(_prefix: Path, *, seen: list[Path]) -> None:
        for _ in range(2):
            vcredist.ensure(
                tmp_path / "prefix",
                profile={},
                cache_dir=cache,
                run_in_prefix=lambda _argv: 0,
                emit=lambda _event: None,
                cancel=Cancel(),
                overridden_fn=lambda: True,
                index_source_fn=lambda: "",
                download_fn=download_fn,
            )

    run_install(tmp_path / "prefix", seen=seen)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    for tmp in seen:
        assert tmp.parent == cache
        assert tmp.name.endswith(".part")


# ---------------------------------------------------------------------------
# 2. lepton check-host: every MISSING block, and ok follows ``missing``
# ---------------------------------------------------------------------------
def test_check_host_ok_sentence_cannot_hide_a_missing_block() -> None:
    text = "MISSING:\n  - bash\nOK: all required pieces present.\n"
    out = lepton.parse_check_host(text)
    assert out["ok"] is False
    assert out["missing"] == ["bash"]


def test_check_host_collects_every_missing_block() -> None:
    text = (
        "MISSING:\n  - bash\n  - curl\n"
        "MISSING:\n  - xdotool\n"
        "getenforce: Permissive\n"
    )
    out = lepton.parse_check_host(text)
    assert out["missing"] == ["bash", "curl", "xdotool"]
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# 3. publish_index: the hashed GET must land on the HEAD's final URL
# ---------------------------------------------------------------------------
def _publish_index_module():
    spec = importlib.util.spec_from_file_location(
        "publish_index_sweep", REPO / "scripts" / "publish_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Resp:
    def __init__(self, body: bytes, *, url: str, modified: str = "M1") -> None:
        self._buf = io.BytesIO(body)
        self.headers = {
            "Content-Length": str(len(body)),
            "Last-Modified": modified,
        }
        self._url = url

    def geturl(self) -> str:
        return self._url

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _split_opener(*, head_url: str, get_url: str, body: bytes, calls=None):
    def opener(req, timeout=None):
        method = getattr(req, "method", None) or "GET"
        if calls is not None:
            calls.append(method)
        return _Resp(body, url=head_url if method == "HEAD" else get_url)

    return opener


def test_publish_vcredist_refuses_a_get_that_ends_elsewhere(tmp_path: Path) -> None:
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    opener = _split_opener(
        head_url="https://download.visualstudio.microsoft.com/a/vc_redist.x64.exe",
        get_url="https://download.visualstudio.microsoft.com/b/vc_redist.x64.exe",
        body=b"A" * 10_000_000,
    )
    assert module.publish_vcredist(out, {}, opener) is False
    assert not (out / "latest.json").exists()


def test_publish_webview2_refuses_a_get_that_ends_elsewhere(tmp_path: Path) -> None:
    module = _publish_index_module()
    out = tmp_path / "webview2"
    opener = _split_opener(
        head_url="https://msedge.sf.dl.delivery.mp.microsoft.com/x/Installer.exe",
        get_url="https://msedge.sf.dl.delivery.mp.microsoft.com/y/Installer.exe",
        body=b"A" * 50_000_000,
    )
    assert module.publish_webview2(out, {}, opener) is False
    assert not (out / "latest.json").exists()


def test_publish_vcredist_refuses_a_non_https_get_final_url(tmp_path: Path) -> None:
    module = _publish_index_module()
    out = tmp_path / "vcredist"
    opener = _split_opener(
        head_url="https://download.visualstudio.microsoft.com/a/vc_redist.x64.exe",
        get_url="http://download.visualstudio.microsoft.com/a/vc_redist.x64.exe",
        body=b"A" * 10_000_000,
    )
    assert module.publish_vcredist(out, {}, opener) is False
    assert not (out / "latest.json").exists()


# ---------------------------------------------------------------------------
# 4a. artwork JSON: no redirects, capped streaming body
# ---------------------------------------------------------------------------
class _JsonStream:
    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self.status_code = status_code
        self._chunks = chunks
        self.headers: dict[str, str] = {}
        self.closed = 0

    def iter_content(self, chunk_size: int = 65536):
        yield from self._chunks

    def close(self) -> None:
        self.closed += 1


class _JsonFake:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


def test_get_json_disables_redirects_and_caps_the_body(monkeypatch) -> None:
    body = b"x" * (artwork.MAX_JSON_BYTES + 1)
    resp = _JsonStream([body])
    fake = _JsonFake(resp)
    monkeypatch.setattr(artwork, "_curl", lambda: fake)

    with pytest.raises(RuntimeError, match="too large"):
        artwork._get_json("https://api.example/x")
    assert fake.calls[0][2]["allow_redirects"] is False
    assert resp.closed == 1


def test_get_json_parses_a_capped_stream(monkeypatch) -> None:
    resp = _JsonStream([b'{"data": 1}'])
    fake = _JsonFake(resp)
    monkeypatch.setattr(artwork, "_curl", lambda: fake)

    assert artwork._get_json("https://api.example/x") == {"data": 1}
    assert resp.closed == 1


# ---------------------------------------------------------------------------
# 4b. updatecheck refuses a redirect that leaves the original host
# ---------------------------------------------------------------------------
def test_default_update_fetch_rejects_off_host_redirects(monkeypatch) -> None:
    handlers = []

    class _Opener:
        def open(self, _request, timeout=None):
            return io.BytesIO(b"{}")

    def build_opener(handler):
        handlers.append(handler)
        return _Opener()

    monkeypatch.setattr(download.urllib.request, "build_opener", build_opener)
    updatecheck._default_fetch("https://forge.example/api/releases/latest", 10)

    assert handlers
    allowed = handlers[0]._redirect_allowed
    assert allowed is not None
    assert allowed("https://forge.example/api/other") is True
    assert allowed("https://evil.example/api/other") is False


# ---------------------------------------------------------------------------
# 4c. artwork: a public-looking name that resolves locally is refused
# ---------------------------------------------------------------------------
def _answers(*addresses: str):
    def resolver(_host, _port=None):
        return [(2, 1, 6, "", (address, 0)) for address in addresses]

    return resolver


def test_resolves_publicly_requires_every_address_to_be_global() -> None:
    assert artwork.resolves_publicly("cdn.example.net", _answers("8.8.8.8")) is True
    assert artwork.resolves_publicly("cdn.example.net", _answers("127.0.0.1")) is False
    assert artwork.resolves_publicly("cdn.example.net", _answers("10.0.0.5")) is False
    assert artwork.resolves_publicly("cdn.example.net", _answers("8.8.8.8", "127.0.0.1")) is False

    def broken(_host, _port=None):
        raise OSError("no such host")

    assert artwork.resolves_publicly("cdn.example.net", broken) is False


def test_download_with_empty_allowlist_refuses_a_local_resolution(
    monkeypatch, tmp_path: Path
) -> None:
    fake = _JsonFake(_JsonStream([b"IMG"]))
    monkeypatch.setattr(artwork, "_curl", lambda: fake)
    monkeypatch.setattr(artwork, "_RESOLVER", _answers("127.0.0.1"))
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)

    assert artwork.download("https://public.example.net/a.jpg", "game", "hero", ()) is None
    assert fake.calls == []


def test_download_with_an_allowlist_does_not_resolve(monkeypatch, tmp_path: Path) -> None:
    fake = _JsonFake(_JsonStream([b"IMG"]))
    monkeypatch.setattr(artwork, "_curl", lambda: fake)
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)

    def unexpected(_host, _port=None):
        raise AssertionError("allowlisted hosts must not be resolved")

    monkeypatch.setattr(artwork, "_RESOLVER", unexpected)
    path = artwork.download(
        "https://cdn.example.net/a.jpg", "game", "hero", ("cdn.example.net",)
    )
    assert path is not None
