# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-tree Flash sweep of b23 (2026-11-11), slices c23, c27, c24, c34: each one reproduced."""

from __future__ import annotations

import http.client
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---- c23: an empty Sophon manifest must not uninstall the game ------------------------------


def test_a_sophon_manifest_without_files_is_refused(monkeypatch):
    zstd = pytest.importorskip("zstandard")
    from nadare.core import sophon

    pkg = sophon.SophonPackage("game", "https://x/m", "https://x/c", "m1", 10_000_000)
    body = zstd.ZstdCompressor().compress(b"")
    monkeypatch.setattr(sophon, "_fetch_bytes", lambda *a, **k: body)
    with pytest.raises(sophon.SophonError, match="no files"):
        sophon._load_manifest(None, pkg)


# ---- c27: the VC++ marker sits in the game-writable prefix ----------------------------------


def test_the_vcredist_marker_never_writes_through_a_link(tmp_path):
    from nadare.core import vcredist

    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / vcredist.FAILED_MARKER).symlink_to(victim)
    vcredist._mark_failed(prefix, kind="cancelled")
    assert victim.read_text() == "precious"
    marker = prefix / vcredist.FAILED_MARKER
    assert marker.is_file() and not marker.is_symlink()


def test_a_huge_vcredist_marker_is_not_read_whole(tmp_path, monkeypatch):
    from nadare.core import vcredist

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / vcredist.FAILED_MARKER).write_text("x" * (2 << 20))
    asked: list[int] = []
    real_read = os.read

    def spy_read(fd, size):
        asked.append(size)
        return real_read(fd, size)

    monkeypatch.setattr("pathlib.Path.read_text", lambda *a, **k: pytest.fail("read whole"))
    monkeypatch.setattr("pathlib.Path.read_bytes", lambda *a, **k: pytest.fail("read whole"))
    monkeypatch.setattr(vcredist.os, "read", spy_read)
    try:
        vcredist._failed_recently(prefix)
    finally:
        monkeypatch.setattr(vcredist.os, "read", real_read)
    assert asked and max(asked) <= 4096


# ---- c24: a truncated read is a transient failure (retried), not a crash --------------------


def test_nikke_fetch_retries_a_truncated_read(monkeypatch):
    import hashlib

    from nadare.core.sources import nikke as source

    data = b"0123456789"
    calls = []

    class Response:
        def __init__(self, truncated):
            self.truncated = truncated

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def geturl(self):
            return "https://cloud.nikke-kr.com/PC/x.wgc"

        def read(self, n):
            if self.truncated:
                raise http.client.IncompleteRead(b"01234", 5)
            return data

    def fake_open(url, **kwargs):
        calls.append(url)
        return Response(truncated=len(calls) == 1)

    monkeypatch.setattr(source.download, "http_open", fake_open)
    monkeypatch.setattr(source, "_retry_sleep", lambda seconds: None)
    got = source.fetch_object(
        "https://cloud.nikke-kr.com/PC/x.wgc", len(data), hashlib.md5(data).hexdigest()
    )
    assert got == data
    assert len(calls) == 2


# ---- c34: a hung notification service must not freeze the window ---------------------------


def test_send_uses_a_short_timeout_and_no_introspection(monkeypatch):
    QtDBus = pytest.importorskip("PyQt6.QtDBus")
    from nadare.gui import notify

    seen = {}

    class Reply:
        def type(self):
            return QtDBus.QDBusMessage.MessageType.ReplyMessage

        def errorName(self):
            return ""

    class Bus:
        def isConnected(self):
            return True

        def call(self, message, mode=None, timeout=-1):
            seen["timeout"] = timeout
            seen["member"] = message.member()
            return Reply()

    def no_interface(*a, **k):
        raise AssertionError("QDBusInterface introspects synchronously")

    monkeypatch.setattr(QtDBus.QDBusConnection, "sessionBus", staticmethod(lambda: Bus()))
    monkeypatch.setattr(QtDBus, "QDBusInterface", no_interface)
    assert notify.send("Nadare", "done") is True
    assert seen["member"] == "Notify"
    assert 0 < seen["timeout"] <= 3000


def test_a_fifo_vcredist_marker_does_not_block(tmp_path):
    """Round 4: the marker lives in the game-writable prefix; a FIFO there hung every launch."""
    import threading

    from nadare.core import vcredist

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    os.mkfifo(prefix / vcredist.FAILED_MARKER)
    answer: list[object] = []
    worker = threading.Thread(
        target=lambda: answer.append(vcredist._failed_recently(prefix)), daemon=True
    )
    worker.start()
    worker.join(5)
    if worker.is_alive():  # unblock the reader so the test process can end
        with open(prefix / vcredist.FAILED_MARKER, "wb"):
            pass
        worker.join(5)
        pytest.fail("reading the VC++ marker blocked on a FIFO")
    assert answer == [None]  # not a marker Nadare wrote: no wait
