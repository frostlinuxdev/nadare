# SPDX-License-Identifier: GPL-3.0-or-later
"""The failures people hit most are said in plain words, with the raw text kept as details."""

from __future__ import annotations

import errno
import os
import socket
import urllib.error

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.core import friendly  # noqa: E402
from nadare.core.backends import BackendError
from nadare.core.download import DownloadError, HTTPStatusError, IntegrityError
from nadare.core.sophon import SophonError

try:  # the real box, before conftest's autouse stub replaces it in every test
    from nadare.gui.game_view import GameView

    _REAL_FAILURE_CHOICE = GameView._failure_choice
except Exception:  # noqa: BLE001 - PyQt6 may be missing; the GUI test then skips
    GameView = None


def _chained(outer: Exception, inner: BaseException) -> Exception:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer from exc
    except Exception as wrapped:
        return wrapped


def test_offline_says_check_the_connection_and_keeps_the_raw_text():
    root = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
    exc = _chained(
        BackendError("could not set up the Proton runner: cannot reach https://x/y"), root
    )
    text = friendly.user_text(exc)
    assert text.startswith("Nadare could not reach the download server")
    assert "online" in text
    assert friendly.DETAILS in text and "cannot reach https://x/y" in text


def test_a_timeout_and_a_dropped_connection_are_network_problems_too():
    for root in (TimeoutError("timed out"), ConnectionResetError(104, "reset")):
        text = friendly.user_text(_chained(DownloadError("download failed"), root))
        assert text.startswith("Nadare could not reach the download server")


def test_http_status_is_explained_by_kind():
    forbidden = HTTPStatusError("HTTP 403 for https://cdn/x", status=403)
    gone = _chained(SophonError("cannot download https://cdn/c: HTTP 404 for https://cdn/c"),
                    SophonError("HTTP 404 for https://cdn/c"))
    busy = DownloadError("HTTP 503 for https://cdn/x")
    slow = HTTPStatusError("HTTP 429 for https://cdn/x", status=429)
    assert "refused" in friendly.user_text(forbidden)
    assert "no longer has this file" in friendly.user_text(gone)
    assert "having problems" in friendly.user_text(busy)
    assert "slow down" in friendly.user_text(slow)


def test_a_full_disk_and_a_forbidden_folder():
    full = _chained(DownloadError("not enough disk space to download game.zip"),
                    OSError(errno.ENOSPC, "No space left on device", "/games/g/game.zip.part"))
    denied = PermissionError(errno.EACCES, "Permission denied", "/games/g/data")
    readonly = OSError(errno.EROFS, "Read-only file system", "/mnt/ro/g")
    assert friendly.user_text(full).startswith("The disk is full")
    assert "/games/g" in friendly.user_text(full)
    for exc in (denied, readonly):
        text = friendly.user_text(exc)
        assert text.startswith("Nadare is not allowed to change")
        assert exc.filename in text


def test_a_damaged_download_says_retry_downloads_it_again():
    for exc in (IntegrityError("sha256 mismatch for game.zip"),
                SophonError("md5 mismatch for chunk 'abc'")):
        assert friendly.user_text(exc).startswith("A downloaded file arrived damaged")


def test_anything_else_is_shown_exactly_as_the_backend_wrote_it():
    for message in ("STOVE asks you to accept the terms first.",
                    "game executable not found: /x/y.exe", "boom"):
        assert friendly.user_text(BackendError(message)) == message
    assert friendly.user_text(ValueError("")) == "ValueError"


def test_split_details():
    assert friendly.split("plain") == ("plain", "")
    head, details = friendly.split(f"Plain words.{friendly.DETAILS}raw: x")
    assert head == "Plain words." and details == "raw: x"


def test_a_message_raised_while_handling_another_error_is_kept():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device", "/x")
        except OSError:
            raise BackendError("STOVE asks you to accept the terms first.")  # noqa: B904
    except BackendError as exc:
        assert friendly.user_text(exc) == "STOVE asks you to accept the terms first."


def test_the_failure_box_shows_the_plain_words_and_hides_the_raw_text_in_details(
    monkeypatch, tmp_path
):
    if GameView is None:
        import pytest

        pytest.skip("PyQt6 is not importable")
    from PyQt6.QtWidgets import QApplication, QMessageBox

    QApplication.instance() or QApplication([])
    seen: dict[str, str] = {}

    def fake_exec(box):
        seen["text"], seen["details"] = box.text(), box.detailedText()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    view = GameView()
    try:
        message = friendly.user_text(HTTPStatusError("HTTP 503 for https://cdn/x", status=503))
        _REAL_FAILURE_CHOICE(view, "Nadare could not finish", message, True)
        assert seen["text"].startswith("The download server is having problems")
        assert "https://cdn/x" not in seen["text"]
        assert seen["details"] == "HTTP 503 for https://cdn/x"
    finally:
        view.deleteLater()
