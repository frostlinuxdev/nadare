# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for three guards that no other test would notice losing (#627)."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from pathlib import Path

import pytest

from nadare.core import download as dl
from nadare.core.config import Config


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int = -1) -> bytes:
        out, self._body = (self._body, b"") if n < 0 else (self._body[:n], self._body[n:])
        return out

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_the_download_swap_is_one_replace_and_the_old_file_is_untouched_until_then(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"new payload" * 100
    dest = tmp_path / "game.bin"
    dest.write_bytes(b"old complete file")
    monkeypatch.setattr(dl, "http_open", lambda *a, **k: _Resp(body))
    seen: list[bytes] = []
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append(Path(dest).read_bytes())  # what a reader sees right up to the swap
        assert Path(src).read_bytes() == body  # the staged file is already complete
        return real_replace(src, dst)

    monkeypatch.setattr(dl.os, "replace", watching_replace)
    dl.download("http://example.invalid/g", dest, expected_size=len(body))

    assert seen == [b"old complete file"]
    assert dest.read_bytes() == body


def test_a_failed_swap_leaves_the_old_download_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"new payload"
    dest = tmp_path / "game.bin"
    dest.write_bytes(b"old complete file")
    monkeypatch.setattr(dl, "http_open", lambda *a, **k: _Resp(body))

    def broken(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dl.os, "replace", broken)
    with pytest.raises(Exception):  # noqa: B017 - whichever error type download() wraps it in
        dl.download("http://example.invalid/g", dest, expected_size=len(body), retries=1)
    assert dest.read_bytes() == b"old complete file"


def test_config_save_waits_for_another_holder_of_the_file_lock(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("one")
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    done = threading.Event()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        thread = threading.Thread(target=lambda: (cfg.save(), done.set()), daemon=True)
        thread.start()
        assert not done.wait(0.5), "save() ran while another process held the config lock"
    finally:
        os.close(fd)  # releases the flock
    assert done.wait(5)
    assert "one" in Config.load(path).profiles


def test_a_chunk_with_a_good_compressed_md5_but_a_bad_plain_md5_is_refused() -> None:
    zstandard = pytest.importorskip("zstandard")
    from nadare.core import sophon

    plain = b"chunk bytes" * 50
    body = zstandard.ZstdCompressor().compress(plain)
    chunk = sophon.ChunkEntry(
        name="c1",
        md5=hashlib.md5(b"something else").hexdigest(),  # wrong uncompressed digest
        offset=0,
        size=len(plain),
        csize=len(body),
        cmd5=hashlib.md5(body).hexdigest(),  # compressed digest is right
    )
    pkg = type("P", (), {"chunk_url": "http://x"})()
    real = sophon._fetch_bytes
    sophon._fetch_bytes = lambda *a, **k: body  # type: ignore[assignment]
    try:
        with pytest.raises(sophon.SophonError, match="md5 mismatch for chunk 'c1'"):
            sophon._fetch_chunk(None, pkg, chunk, None)  # type: ignore[arg-type]
    finally:
        sophon._fetch_bytes = real  # type: ignore[assignment]
