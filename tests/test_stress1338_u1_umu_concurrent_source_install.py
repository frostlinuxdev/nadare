# SPDX-License-Identifier: GPL-3.0-or-later
"""Two launches setting up the (unbundled) managed umu must not break each other (#1338)."""

from __future__ import annotations

import io
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

from nadare.core import umu


def _payload() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("umu/umu-run")
        data = b"#!/usr/bin/python3\n"
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _ArchivePath:
    """Path proxy that lets the test see when the loser's archive is unlinked."""

    def __init__(self, path: Path, unlinked: threading.Event) -> None:
        self._path = path
        self._unlinked = unlinked

    def __fspath__(self) -> str:
        return str(self._path)

    def unlink(self, missing_ok: bool = False) -> None:
        self._path.unlink(missing_ok=missing_ok)
        if threading.current_thread().name == "B":
            self._unlinked.set()


def test_two_source_installs_share_the_archive_without_one_deleting_it(
    tmp_path, monkeypatch
) -> None:
    payload = _payload()
    monkeypatch.setattr(umu, "managed_path", lambda: tmp_path / "data" / "umu-run")
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", tmp_path / "no-bundled-umu.tar")
    monkeypatch.setattr(umu.os.path, "isfile", lambda p: True if str(p) == umu.SHEBANG_PYTHON
                        else Path(p).is_file())
    monkeypatch.setattr(umu.shutil, "which", lambda *_a, **_k: "/usr/bin/python3")
    monkeypatch.setattr(umu, "_smoke_test", lambda *_a, **_k: None)

    both_checked = threading.Barrier(2, timeout=5)
    b_unlinked = threading.Event()
    real_extract = umu._extract_member

    def managed():
        both_checked.wait()
        return None

    monkeypatch.setattr(umu, "managed", managed)

    def fake_download(_url, dest, **_kwargs):
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return SimpleNamespace(path=_ArchivePath(path, b_unlinked))

    monkeypatch.setattr(umu.download, "download", fake_download)

    def extract(archive, destination, check=None):
        if threading.current_thread().name == "A":
            # A reaches extraction, then B finishes and deletes the shared archive.
            assert b_unlinked.wait(5)
        return real_extract(archive, destination, check=check)

    monkeypatch.setattr(umu, "_extract_member", extract)

    results: list[Path] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(umu.ensure_managed())
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    a = threading.Thread(target=worker, name="A")
    b = threading.Thread(target=worker, name="B")
    a.start()
    b.start()
    a.join(10)
    b.join(10)

    assert not a.is_alive() and not b.is_alive()
    assert errors == []
    assert len(results) == 2 and all(p.is_file() for p in results)
