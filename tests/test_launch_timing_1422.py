# SPDX-License-Identifier: GPL-3.0-or-later
"""#1422: locate slow Play steps and release background media promptly."""

from __future__ import annotations

import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context
from PyQt6.QtWidgets import QApplication

from nadare.backends import launcher
from nadare.core import artwork, events, logview, news, report, runlog, yostar
from nadare.core.config import Config
from nadare.gui import workers
from nadare.gui.logs_dialog import LogsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(workers.time, "monotonic", lambda: now[0])
    return now


def _worker(tmp_path, operation="launch"):
    return workers.BackendWorker(Config.load(tmp_path / "config.json"), "g", operation)


@pytest.fixture
def timed_log(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    worker = _worker(tmp_path)
    worker._open_log()
    clock[0] += 12.3
    worker._note("[log] checking\npassword=private\n[end] exit=0 after 12s")
    worker._close_log()
    return next((tmp_path / "logs" / "g").glob("launch-*.log"))


def test_note_times_every_line_from_log_open(timed_log):
    lines = timed_log.read_text().splitlines()
    assert lines[1].startswith("===== ") and "launch (g) | nadare" in lines[1]
    assert lines[2:] == [
        "+  12.3s [log] checking",
        "+  12.3s password=<redacted>",
        "+  12.3s [end] exit=0 after 12s",
    ]


@pytest.mark.parametrize("operation", ["news", "status", "media"])
def test_background_note_keeps_original_format(tmp_path, monkeypatch, clock, operation):
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    worker = _worker(tmp_path, operation)
    worker._open_log()
    clock[0] += 12.3
    worker._note("[log] checking\nsecond line")
    worker._close_log()
    assert (tmp_path / "logs/g/background.log").read_text().splitlines()[2:] == [
        "[log] checking", "second line"
    ]


def test_runlog_reader_preserves_timed_lines(timed_log):
    assert runlog.operation_of(timed_log) == "launch"
    assert runlog.trim_rolling(timed_log, max_bytes=80)
    assert "+  12.3s [end] exit=0 after 12s" in timed_log.read_text()


def test_logview_reader_preserves_timed_lines(timed_log):
    text = logview.read_tail(timed_log)
    assert logview.has_session_header(text)
    assert len(logview.sessions(text)) == 2
    assert logview.filter_lines(text, "checking") == "+  12.3s [log] checking\n"


def test_diagnostics_reader_preserves_timed_lines(timed_log):
    assert report._newest_runs(timed_log.parent) == [timed_log]
    assert "+  12.3s [end] exit=0 after 12s" in report._tail(timed_log)


def test_bundle_reader_preserves_timed_lines(timed_log, tmp_path):
    target = tmp_path / "logs.zip"
    assert logview.bundle(["g"], tmp_path / "logs", target) == 1
    with zipfile.ZipFile(target) as bundle:
        text = bundle.read(f"g/{timed_log.name}").decode()
    assert "+  12.3s [log] checking" in text
    assert "private" not in text


def test_mask_preserves_elapsed_and_summary(timed_log):
    assert "+  12.3s [end] exit=0 after 12s" in events.mask(timed_log.read_text())
    assert "private" not in timed_log.read_text()


def test_gui_reader_preserves_timed_lines(qapp, timed_log, tmp_path):
    dialog = LogsDialog(["g"], profile="g", directory=tmp_path / "logs", follow=False)
    try:
        assert "+  12.3s [log] checking" in dialog.view.toPlainText()
        dialog.search.setText("after 12s")
        dialog._render()
        assert "+  12.3s [end] exit=0 after 12s" in dialog.current_text()
    finally:
        dialog.close()


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    # Use a unique source so tests do not share an in-process media cache entry.
    source = str(tmp_path / "media.json")
    monkeypatch.setattr(yostar, "configured_media", lambda profile: source)
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(yostar, "cache_media", lambda *a, **k: {"banners": []})

    class Backend:
        game_tag = "arknights"
        ctx = SimpleNamespace(profile={"backend": "arknights-yostar"})

    return source, Backend()


def _media_payload():
    return {"game_tag": "arknights", "banners": []}


def test_cancelled_media_never_requests(monkeypatch, media_env):
    source, _ = media_env
    token = events.Cancel()
    token.cancel()
    calls = []
    monkeypatch.setattr(yostar, "_read_json", lambda *a: calls.append(a))
    with pytest.raises(events.Cancelled):
        yostar.load_media(source, cancel=token)
    assert calls == []


def test_media_index_checks_cancel_before_nested_request(monkeypatch, media_env):
    source, _ = media_env
    token = events.Cancel()
    calls = []

    def read(source, timeout):
        calls.append(source)
        token.cancel()
        return {"arknights": {"url": "manifest.json", "media": "game.json"}}

    monkeypatch.setattr(yostar, "_read_json", read)
    with pytest.raises(events.Cancelled):
        yostar.load_media(source, game_tag="arknights", cancel=token)
    assert calls == [source]


def test_worker_media_cache_expires(tmp_path, monkeypatch, clock, media_env):
    source, backend = media_env
    calls = []
    monkeypatch.setattr(yostar, "_read_json", lambda *a: calls.append(a) or _media_payload())
    for _ in range(2):
        _worker(tmp_path, "media")._load_media(backend)
    assert len(calls) == 1
    clock[0] += news.CACHE_TTL
    _worker(tmp_path, "media")._load_media(backend)
    assert len(calls) == 2
    # The socket timeout is the whole remaining budget, not 1 s: _await already returns at the
    # deadline or on cancel, and a 1 s timeout would fail a slow TLS connect (no banners).
    assert calls[0] == (source, news.NEWS_DEADLINE)


def test_failed_media_is_retried_then_cached(tmp_path, monkeypatch, media_env):
    _, backend = media_env
    calls = []

    def read(*args):
        calls.append(args)
        if len(calls) == 1:
            raise yostar.ManifestError("offline")
        return _media_payload()

    monkeypatch.setattr(yostar, "_read_json", read)
    for _ in range(3):
        _worker(tmp_path, "media")._load_media(backend)
    assert len(calls) == 2


def test_media_cache_separates_sources_and_game_tags(monkeypatch, media_env):
    source, _ = media_env
    calls = []

    def read(url, timeout):
        calls.append(url)
        if url == source:
            return {tag: {"url": "manifest.json", "media": f"{tag}.json"}
                    for tag in ("arknights", "other")}
        return {"game_tag": "other" if url.endswith("other.json") else "arknights"}

    monkeypatch.setattr(yostar, "_read_json", read)
    for tag in ("arknights", "other", "arknights"):
        assert yostar.load_media(source, game_tag=tag).game_tag == tag
    assert len(calls) == 4


@pytest.mark.parametrize("stage", ["document", "images", "fallback"])
def test_worker_cancels_during_blocked_media(tmp_path, monkeypatch, media_env, stage):
    _, backend = media_env
    entered = threading.Event()
    release = threading.Event()
    worker = _worker(tmp_path, "media")
    calls = []

    def block(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {"banners": []}

    def read(source, timeout):
        calls.append(timeout)
        if stage == "document":
            block()
        return _media_payload()

    if stage == "images":
        monkeypatch.setattr(yostar, "cache_media", block)
    elif stage == "fallback":
        monkeypatch.setattr(artwork, "fetch", block)

    def cancel():
        if entered.wait(2):
            worker.cancel()

    monkeypatch.setattr(yostar, "_read_json", read)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    started = time.monotonic()
    try:
        with pytest.raises(events.Cancelled):
            worker._load_media(backend)
        assert time.monotonic() - started < 1.0
        assert len(calls) == 1 and calls[0] >= news.NEWS_DEADLINE - 1
    finally:
        release.set()
        canceller.join()


def test_nikke_emits_one_step_before_update_checks(tmp_path, monkeypatch):
    backend = launcher.NikkeBackend(make_context(tmp_path, "launcher-nikke"))
    seen = []
    backend.ctx.emit = seen.append
    monkeypatch.setattr(launcher, "scan_pids", lambda *a, **k: [])
    monkeypatch.setattr(backend, "_client_missing_but_signed_in", lambda: False)

    def check():
        assert [(e.kind, e.message) for e in seen] == [
            ("step", "NIKKE: checking for a game update")
        ]

    monkeypatch.setattr(backend, "_fetch_client_latest", lambda: check() or {})
    monkeypatch.setattr(backend, "_client_outdated", lambda latest: False)
    monkeypatch.setattr(backend, "_update_launcher_if_needed", lambda **k: None)
    backend._update_before_play()
    assert len(seen) == 1
