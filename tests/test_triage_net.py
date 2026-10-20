# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the core-hardening triage batch.

Covers: #12 (private dirs/log mode), #21 (symlinked ``.part``), #30 (credential
env denylist), #45 (cyclic media index), #46 (corrupt resumable ``.part``) and
#77 (URL userinfo redaction).  #23 is out of scope (FGO is not touched).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from nadare.core import download as dl
from nadare.core import logging as nadare_logging
from nadare.core import paths, sandbox, yostar
from nadare.core.events import mask


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}
        self._pos = 0

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        end = len(self._body) if n is None or n < 0 else self._pos + n
        data = self._body[self._pos : end]
        self._pos += len(data)
        return data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_http(body: bytes):
    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return _FakeResponse(body, headers={"Content-Length": str(len(body))})

    return _open


class _FakeStreamResponse:
    """Stand-in for ``http.client.HTTPResponse`` used by ``HttpClientPool``."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self._body = body
        self._pos = 0

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, n: int = -1) -> bytes:
        end = len(self._body) if n is None or n < 0 else self._pos + n
        data = self._body[self._pos : end]
        self._pos += len(data)
        return data

    def close(self) -> None:
        pass


class _FakePool:
    def __init__(self, *responses: _FakeStreamResponse):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open(self, url: str, **kwargs: object) -> dl.HttpResponse:
        self.calls.append((url, kwargs))
        return dl.HttpResponse(self._responses.pop(0))


# ---------------------------------------------------------------------------
# #21: a symlink planted at ``<dest>.part`` must never be written through
# ---------------------------------------------------------------------------
def test_download_does_not_write_through_symlinked_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"official payload" * 32
    dest = tmp_path / "game.bin"
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"SECRET-ORIGINAL")
    part = tmp_path / "game.bin.part"
    part.symlink_to(victim)

    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dl.download(
        "http://example.invalid/game",
        dest,
        expected_size=len(body),
        expected_sha256=_sha256(body),
    )

    assert victim.read_bytes() == b"SECRET-ORIGINAL", "victim file was clobbered"
    assert dest.read_bytes() == body
    assert not dest.is_symlink()
    assert not part.is_symlink()


def test_download_does_not_follow_dangling_symlinked_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"payload"
    dest = tmp_path / "game.bin"
    victim = tmp_path / "gone.bin"
    (tmp_path / "game.bin.part").symlink_to(victim)

    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dl.download(
        "http://example.invalid/game",
        dest,
        expected_size=len(body),
        expected_sha256=_sha256(body),
    )

    assert not victim.exists(), "download created the symlink target"
    assert dest.read_bytes() == body


def test_download_does_not_reuse_a_symlinked_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"verified-content"
    dest = tmp_path / "game.bin"
    victim = tmp_path / "victim.bin"
    victim.write_bytes(body)  # exactly matches the expected hash/size
    (tmp_path / "game.bin.part").symlink_to(victim)

    seen: list[dict[str, str]] = []

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        seen.append(dict(headers or {}))
        return _FakeResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", _open)
    dl.download(
        "http://example.invalid/game",
        dest,
        expected_size=len(body),
        expected_sha256=_sha256(body),
    )

    assert not dest.is_symlink(), "destination became a symlink to the victim"
    assert dest.read_bytes() == body
    assert seen, "a symlinked part must be refetched, not reused"


def test_stream_download_does_not_write_through_symlinked_part(tmp_path: Path) -> None:
    body = b"streamed" * 20
    dest = tmp_path / "f.bin"
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"keep-me")
    part = tmp_path / "f.bin.part"
    part.symlink_to(victim)

    resp = _FakeStreamResponse(200, {"Content-Length": str(len(body))}, body)
    dl.stream_download(
        "https://x/y", dest, pool=_FakePool(resp), expected_size=len(body)
    )

    assert victim.read_bytes() == b"keep-me", "victim file was clobbered"
    assert part.read_bytes() == body
    assert not part.is_symlink()


# ---------------------------------------------------------------------------
# #46: a stale/corrupt resumable ``.part`` must be refetched, not fatal
# ---------------------------------------------------------------------------
def test_download_restarts_after_corrupt_resumable_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = bytes(range(256)) * 8  # 2048 bytes
    half = len(body) // 2
    dest = tmp_path / "game.bin"
    part = tmp_path / "game.bin.part"
    part.write_bytes(b"X" * half)  # wrong prefix, plausible resume length
    ranges: list[str | None] = []

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        headers = dict(headers or {})
        ranges.append(headers.get("Range"))
        if "Range" in headers:
            offset = int(headers["Range"].split("=")[1].rstrip("-"))
            tail = body[offset:]
            return _FakeResponse(
                tail,
                status=206,
                headers={
                    "Content-Length": str(len(tail)),
                    "Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}",
                },
            )
        return _FakeResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", _open)
    result = dl.download(
        "http://example.invalid/game",
        dest,
        expected_size=len(body),
        expected_sha256=_sha256(body),
        retries=1,
        backoff=0,
    )

    assert result.size == len(body)
    assert dest.read_bytes() == body
    assert ranges[0] == f"bytes={half}-"
    assert ranges[-1] is None, "the corrupt prefix was not refetched from zero"


# ---------------------------------------------------------------------------
# #45: a cyclic media index must raise MediaError, not RecursionError
# ---------------------------------------------------------------------------
def test_load_media_cyclic_index_raises_media_error(tmp_path: Path) -> None:
    index = {"Arknights_EN": {"url": "index.json", "media": "index.json"}}
    path = tmp_path / "index.json"
    path.write_text(json.dumps(index))

    with pytest.raises(yostar.MediaError):
        yostar.load_media(str(path), game_tag="Arknights_EN")


# ---------------------------------------------------------------------------
# #77: URL userinfo passwords must be redacted
# ---------------------------------------------------------------------------
def test_mask_redacts_url_userinfo_password() -> None:
    masked = mask("fetch https://alice:s3cr3tpass@example.com/x")
    assert "s3cr3tpass" not in masked
    assert "alice" not in masked
    assert "example.com" in masked


def test_mask_leaves_credential_free_urls_untouched() -> None:
    for url in ("https://example.com/x", "https://user@example.com/x"):
        assert mask(url) == url


# ---------------------------------------------------------------------------
# #30: the credential env denylist must cover API keys and session material
# ---------------------------------------------------------------------------
def test_host_argv_refuses_api_key_and_session_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    for name in ("OPENAI_API_KEY", "AWS_ACCESS_KEY", "COOKIE_JAR", "SESSION_KEY"):
        monkeypatch.delenv(name, raising=False)
        with pytest.raises(ValueError):
            sandbox.host_argv(["tool"], env={name: "value"})


def test_host_argv_still_allows_benign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.delenv("NADARE_DISPLAY", raising=False)
    argv = sandbox.host_argv(["tool"], env={"NADARE_DISPLAY": ":0"})
    assert argv == ["flatpak-spawn", "--host", "--env=NADARE_DISPLAY=:0", "tool"]
    # Session-bus/desktop session variables are not credentials.
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XDG_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
        sandbox.host_argv(["tool"], env={name: "x"})


# ---------------------------------------------------------------------------
# #12: Nadare's own dirs and the log file must be private
# ---------------------------------------------------------------------------
def test_ensure_dirs_creates_private_directories() -> None:
    old = os.umask(0o002)
    try:
        paths.ensure_dirs()
    finally:
        os.umask(old)
    for directory in (
        paths.data_dir(),
        paths.config_dir(),
        paths.state_dir(),
        paths.cache_dir(),
        paths.logs_dir(),
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_log_file_is_private(tmp_path: Path) -> None:
    nadare_logging.reset()
    log_file = tmp_path / "mode.log"
    logger = nadare_logging.setup(filename=log_file)
    logger.info("probe")
    for handler in list(logger.handlers):
        handler.flush()
    try:
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    finally:
        nadare_logging.reset()
