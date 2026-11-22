# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Download concurrency and the shared speed budget, using only localhost."""

import hashlib
import http.server
import io
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from nadare.core import crc64
from nadare.core import directdl as dd
from nadare.core import download as dl
from nadare.core.events import Cancel, Cancelled


@contextmanager
def cdn(data, ranges=True, accept_ranges=True):
    state = {"active": 0, "peak": 0, "download_peak": 0, "requests": []}
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            value = data[self.path]
            header = self.headers.get("Range")
            probe = header == "bytes=0-0"
            start, end = 0, len(value) - 1
            if ranges and header:
                a, b = header.removeprefix("bytes=").split("-")
                start, end = int(a), int(b) if b else end
            with lock:
                state["requests"].append((self.path, header))
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if not probe:
                    state["download_peak"] = max(state["download_peak"], state["active"])
            try:
                self.send_response(206 if ranges and header else 200)
                if ranges and accept_ranges:
                    self.send_header("Accept-Ranges", "bytes")
                if ranges and header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(value)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                time.sleep(0.04)
                for i in range(start, end + 1, 65536):
                    self.wfile.write(value[i : min(i + 65536, end + 1)])
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

    if os.environ.get("NADARE_TEST_MEMORY_TRANSPORT"):
        try:
            from nadare.core.transfer import Response
        except ImportError:

            def Response(body, url):
                return body

        class Body(io.BytesIO):
            def __init__(self, value, start, end, partial):
                super().__init__(value[start : end + 1])
                self.status = 206 if partial else 200
                self.headers = {"Content-Length": str(end - start + 1)}
                if ranges and accept_ranges:
                    self.headers["Accept-Ranges"] = "bytes"
                if partial:
                    self.headers["Content-Range"] = f"bytes {start}-{end}/{len(value)}"
                self.delayed = False
                self.closed_once = False

            def header(self, name, default=None):
                return self.headers.get(name, default)

            def read(self, amount=-1):
                if not self.delayed:
                    self.delayed = True
                    time.sleep(0.04)
                return super().read(amount)

            def close(self):
                if not self.closed_once:
                    self.closed_once = True
                    with lock:
                        state["active"] -= 1
                super().close()

        def open_body(url, **kwargs):
            path = url.removeprefix("http://127.0.0.1")
            value = data[path]
            header = (kwargs.get("headers") or {}).get("Range")
            probe = header == "bytes=0-0"
            start, end = 0, len(value) - 1
            if ranges and header:
                a, b = header.removeprefix("bytes=").split("-")
                start, end = int(a), int(b) if b else end
            with lock:
                state["requests"].append((path, header))
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if not probe:
                    state["download_peak"] = max(state["download_peak"], state["active"])
            return Response(Body(value, start, end, ranges and bool(header)), url)

        state["url"] = "http://127.0.0.1"
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dl, "http_open", open_body)
            patch.setattr(dl.HttpClientPool, "open", lambda self, url, **kw: open_body(url, **kw))
            yield state
        return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_profile_cap_and_worker_ranges():
    from nadare.backends.arknights import _clamp_workers
    from nadare.core.config import TOP_DEFAULTS
    from nadare.gui.settings_parts.options import INT_RANGES

    assert INT_RANGES["download_workers"] == (1, 32)
    assert _clamp_workers(99) == 32
    assert TOP_DEFAULTS["download_speed_limit"] == 0
    plan = dd.Plan("test", "1", extra={"workers": 32})
    # A profile used to replace the plan default; now it only caps it.
    assert dd.worker_cap(plan, 16) == 16
    assert dd.worker_cap(plan, None) == 32
    assert dd.worker_cap(dd.Plan("test", "1"), 32) == 6
    assert dd.worker_cap(plan, "bad") == 32


def test_multipart_parallel_order_and_summary(tmp_path):
    # Each piece must span more than one read chunk (1 MiB): a piece read whole in one call closes
    # its connection before the progress event, and "connections > 0" would then depend on timing.
    content = b"ordered archive contents" * 200000
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("game.bin", content)
    archive = buffer.getvalue()
    cut = len(archive) // 3
    pieces = [archive[:cut], archive[cut : cut * 2], archive[cut * 2 :]]
    events = []
    with cdn({f"/{i}": v for i, v in enumerate(pieces)}) as s:
        parts = tuple(
            dd.RemoteFile(f"p{i}", s["url"] + f"/{i}", len(v)) for i, v in enumerate(pieces)
        )
        source = dd.RemoteFile(
            "all.zip", "", len(archive), sha256=hashlib.sha256(archive).hexdigest()
        )
        plan = dd.Plan("test", "1", archives=(dd.Archive("pack", source, parts=parts),))
        dd.sync(plan, tmp_path, workers=3, emit=events.append)
        assert s["peak"] >= 2
    assert min(len(v) for v in pieces) > dl.DEFAULT_CHUNK
    assert (tmp_path / "game.bin").read_bytes() == content
    summary = [e for e in events if e.data.get("download_summary")]
    assert len(summary) == 1
    assert summary[0].data["workers_requested"] == 3
    assert summary[0].data["connections_opened"] >= 3
    assert summary[0].data["bytes"] == len(archive)
    assert summary[0].data["seconds"] > 0
    assert summary[0].data["average_mib_s"] > 0
    assert summary[0].data["hosts"] == ["127.0.0.1"]
    assert any(e.data.get("connections", 0) > 0 for e in events if e.kind == "progress")


@pytest.mark.parametrize("ranges", [True, False])
def test_ranged_identical_and_fallback(tmp_path, monkeypatch, ranges):
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 100000, raising=False)
    data = bytes(range(256)) * 8192
    with cdn({"/file": data}, ranges=ranges) as s:
        dl.download(s["url"] + "/file", tmp_path / "file", expected_size=len(data), range_workers=4)
        assert (tmp_path / "file").read_bytes() == data
        if ranges:
            assert s["peak"] >= 2
            assert len([r for _, r in s["requests"] if r]) >= 4
        else:
            # A rejected probe can still be winding down on the HTTP server.
            assert s["requests"] == [("/file", "bytes=0-0"), ("/file", None)]


def test_ranges_cancel_keeps_finished_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 100000, raising=False)
    data = bytes(range(251)) * 16384
    cancel = Cancel()

    def stop(event):
        if event.data.get("range_finished") == 0:
            cancel.cancel()

    target = tmp_path / "file"
    with cdn({"/file": data}) as s:
        with pytest.raises(Cancelled):
            dl.download(
                s["url"] + "/file",
                target,
                expected_size=len(data),
                range_workers=4,
                cancel=cancel,
                emit=stop,
            )
        finished = list(tmp_path.glob("file.part-*.done"))
        assert finished
        before = len(s["requests"])
        dl.download(s["url"] + "/file", target, expected_size=len(data), range_workers=4)
        new = s["requests"][before:]
        assert not any(r == f"bytes=0-{len(data) // 4 - 1}" for _, r in new)
    assert target.read_bytes() == data
    assert not list(tmp_path.glob("file.part-*"))


def test_global_limit_eight_workers_and_live_change(tmp_path, record_property):
    from nadare.core.transfer import limiter

    data = b"x" * 1_875_000
    with cdn({"/file": data}) as s:
        limiter.set_limit(5)
        start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(
                    pool.map(lambda i: dl.download(s["url"] + "/file", tmp_path / str(i)), range(8))
                )
            elapsed = time.monotonic() - start
            record_property("limited_seconds", elapsed)
            record_property("limited_MB_s", 15 / elapsed)
            assert 15_000_000 / elapsed <= 5_000_000 * 1.15
            assert elapsed < 12
            limiter.set_limit(0.01)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(dl.download, s["url"] + "/file", tmp_path / "live")
                time.sleep(0.2)
                start = time.monotonic()
                limiter.set_limit(0)
                future.result(timeout=2)
                assert time.monotonic() - start < 2
        finally:
            limiter.set_limit(0)
    assert all((tmp_path / str(i)).read_bytes() == data for i in range(8))


def test_progress_coalesced_and_crc_fallback_once(tmp_path, monkeypatch, caplog, record_property):
    data = b"a" * (4 << 20)
    events = []
    with cdn({"/file": data}) as s:
        plan = dd.Plan(
            "test",
            "1",
            files=tuple(dd.RemoteFile(str(i), s["url"] + "/file", len(data)) for i in range(8)),
        )
        start = time.monotonic()
        dd.sync(plan, tmp_path, workers=8, emit=events.append)
        elapsed = time.monotonic() - start
    progress = [e for e in events if e.kind == "progress" and e.data.get("stage") == "download"]
    record_property("progress_events", len(progress))
    record_property("transfer_seconds", elapsed)
    assert len(progress) <= 2 + elapsed * 5
    assert any(e.data.get("connections", 0) > 0 for e in progress)
    monkeypatch.setattr(crc64, "_NATIVE_CRC64", None)
    monkeypatch.setattr(crc64, "_fallback_logged", False, raising=False)
    assert crc64.crc64(b"123456789") == 0x995DC9BBDF1939FA
    crc64.crc64(b"again")
    assert sum("pure Python" in r.message for r in caplog.records) == 1


def test_settings_limit_persisted_and_live(tmp_path):
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.core.transfer import limiter
    from nadare.gui.settings import SettingsPage

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    page = SettingsPage()
    page.load_app_only(config)
    assert page.download_speed_limit.value() == 0
    page.download_speed_limit.setValue(5)
    page.save()
    assert config.data["download_speed_limit"] == 5
    assert limiter.limit_mb_s == 5
    assert Config.load(config.path).data["download_speed_limit"] == 5
    page.load_app_only(config)
    assert page.download_speed_limit.value() == 5
    page.resize(1050, 900)
    page.show()
    app.processEvents()
    assert page.grab().save(str(tmp_path / "settings-limit.png"))
    page.download_speed_limit.setValue(0)
    page.save()
    assert limiter.limit_mb_s == 0
    page.deleteLater()


def test_response_short_reads_and_unlimited(monkeypatch):
    from nadare.core import transfer

    class Short(io.BytesIO):
        def read(self, amount=-1):
            return super().read(min(amount, 3))

    charges = []
    monkeypatch.setattr(transfer.limiter, "take", lambda size, cancel: charges.append(size))
    with transfer.Response(Short(b"abcdefgh"), "http://localhost/a") as response:
        assert response.read(5) == b"abcde"
        assert response.read() == b"fgh"
    assert sum(charges) == 8


def test_unlimited_budget_does_not_open_its_file(tmp_path, monkeypatch):
    """Unlimited is the default: no lock or JSON parse per 64 KiB read."""
    from nadare.core import transfer

    bucket = transfer.TokenBucket()
    bucket.attach(tmp_path / "budget")
    bucket.set_limit(0)

    def touched():
        raise AssertionError("the budget file was opened while unlimited")

    monkeypatch.setattr(bucket, "_state", touched)
    started = time.monotonic()
    bucket.take(64 << 20)  # 64 MiB through an unlimited bucket
    assert time.monotonic() - started < 0.1


def test_ranges_without_accept_ranges_header(tmp_path, monkeypatch):
    """A valid 206/Content-Range is enough; Accept-Ranges is not the only signal."""
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 100000, raising=False)
    data = bytes(range(256)) * 4096
    with cdn({"/file": data}, accept_ranges=False) as s:
        dl.download(
            s["url"] + "/file", tmp_path / "file", expected_size=len(data), range_workers=4
        )
        assert (tmp_path / "file").read_bytes() == data
        assert s["download_peak"] >= 2
        assert len([r for _, r in s["requests"] if r and r != "bytes=0-0"]) >= 4


def test_range_server_changes_support(tmp_path, monkeypatch):
    from nadare.core import transfer

    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 10)
    data = b"contents" * 100
    requests = []

    class Body(io.BytesIO):
        def __init__(self, header):
            probe = header == "bytes=0-0"
            super().__init__(data[:1] if probe else data)
            self.status = 206 if probe else 200
            self.headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(1 if probe else len(data)),
            }
            if probe:
                self.headers["Content-Range"] = f"bytes 0-0/{len(data)}"

    def open_body(url, **kwargs):
        header = (kwargs.get("headers") or {}).get("Range")
        requests.append(header)
        return transfer.Response(Body(header), url)

    monkeypatch.setattr(dl.HttpClientPool, "open", lambda self, url, **kw: open_body(url, **kw))
    monkeypatch.setattr(dl, "http_open", open_body)
    dl.download(
        "http://localhost/a", tmp_path / "a", expected_size=len(data), range_workers=4, retries=1
    )
    assert (tmp_path / "a").read_bytes() == data
    assert requests.count(None) == 1


def test_sophon_throttle_cancel_is_forwarded(monkeypatch):
    from nadare.core import sophon, transfer

    cancel = Cancel()
    cancel.cancel()
    response = transfer.Response(io.BytesIO(b"x" * 100), "http://localhost/chunk")
    pool = type("Pool", (), {"open": lambda self, url: response})()
    # Cancel while reading, after Sophon's initial cancellation check.
    active = Cancel()
    reads = []

    def read_body(self, size):
        reads.append(size)
        active.cancel()
        return b"x"

    response._response = type(
        "Body",
        (),
        {
            "status": 200,
            "read": read_body,
            "close": lambda self: None,
        },
    )()
    with pytest.raises(Cancelled):
        sophon._fetch_bytes(pool, "http://localhost/chunk", 100, active)
    assert len(reads) == 1


def test_speed_control_next_to_workers(tmp_path):
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.add_profile("game", backend="direct-snowbreak")
    page = SettingsPage()
    page.load(config, "game")
    assert page._speed_editors
    page._speed_editors[0].setValue(7.5)
    assert page.download_speed_limit.value() == 7.5
    page.download_speed_limit.setValue(3)
    assert page._speed_editors[0].value() == 3
    # A Save on a game page writes the global value too: its row is right there.
    page.save()
    from nadare.core.transfer import limiter

    assert config.data["download_speed_limit"] == 3
    assert limiter.limit_mb_s == 3
    page.download_speed_limit.setValue(0)
    page.save()
    assert limiter.limit_mb_s == 0
    page.download_speed_limit.setValue(3)
    page.tabs.setCurrentIndex(1)
    page.show_advanced.setChecked(True)
    page.resize(1100, 1200)
    page.show()
    app.processEvents()
    assert page.grab().save(str(tmp_path / "settings-workers-limit.png"))
    page.download_speed_limit.setValue(0)
    page.save()
    assert limiter.limit_mb_s == 0
    page.deleteLater()


def test_stream_and_sophon_charge_shared_bucket(tmp_path, monkeypatch):
    from nadare.core import sophon, transfer

    charges = []
    monkeypatch.setattr(transfer.limiter, "take", lambda size, cancel: charges.append(size))
    data = b"a" * 200000
    with cdn({"/file": data}) as s:
        with dl.HttpClientPool() as pool:
            dl.stream_download(
                s["url"] + "/file", tmp_path / "stream", pool=pool, expected_size=len(data)
            )
            assert sophon._fetch_bytes(pool, s["url"] + "/file", len(data)) == data
    assert sum(charges) == len(data) * 2


def test_budget_shared_across_processes(tmp_path):
    import subprocess
    import sys

    from nadare.core.transfer import TokenBucket

    path = tmp_path / "budget"
    bucket = TokenBucket()
    bucket.attach(path)
    bucket.set_limit(1)
    start = time.monotonic()
    script = (
        "from nadare.core.transfer import TokenBucket; "
        f"b=TokenBucket(); b.attach({str(path)!r}); b.take(1000000)"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        bucket.take(1000000)
        child.wait(timeout=5)
        assert child.returncode == 0
        assert time.monotonic() - start >= 1.8
        bucket.set_limit(0.001)
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(0.15)
        bucket.set_limit(0)
        child.wait(timeout=2)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_czn_passes_shared_budget_to_child(tmp_path):
    from types import SimpleNamespace

    from nadare.backends.czn.backend_parts.streaming import StreamingMixin
    from nadare.core.config import Config

    config = Config.load(tmp_path / "config.json")
    seen = []
    backend = type(
        "Backend",
        (StreamingMixin,),
        {
            "_env": lambda self: {},
            "_stream": lambda self, argv, env, **kw: seen.append(env) or 0,
        },
    )()
    backend.ctx = SimpleNamespace(config=config)
    backend._run_module("nadare.backends.czn.update", ["update"])
    assert seen[0]["NADARE_DOWNLOAD_BUDGET"]


def test_multipart_cancel_reuses_verified_parts(tmp_path):
    content = b"resume archive" * 50000
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("game.bin", content)
    body = buffer.getvalue()
    cut = len(body) // 3
    pieces = [body[:cut], body[cut : cut * 2], body[cut * 2 :]]
    cancel = Cancel()

    def stop(event):
        if event.message.startswith("verified p0"):
            cancel.cancel()

    with cdn({f"/{i}": value for i, value in enumerate(pieces)}) as s:
        parts = tuple(
            dd.RemoteFile(f"p{i}", s["url"] + f"/{i}", len(v), sha256=hashlib.sha256(v).hexdigest())
            for i, v in enumerate(pieces)
        )
        plan = dd.Plan(
            "test",
            "1",
            archives=(dd.Archive("pack", dd.RemoteFile("all.zip", "", len(body)), parts=parts),),
        )
        with pytest.raises(Cancelled):
            dd.sync(plan, tmp_path, workers=3, cancel=cancel, emit=stop)
        before = len(s["requests"])
        assert list((tmp_path / dd.DL_DIR).glob("p0"))
        dd.sync(plan, tmp_path, workers=3)
        assert not any(path == "/0" for path, header in s["requests"][before:])
    assert (tmp_path / "game.bin").read_bytes() == content


def test_range_layout_change_cleans_old_finished_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 100000)
    data = b"x" * (1 << 20)
    cancel = Cancel()

    def stop(event):
        if "range_finished" in event.data:
            cancel.cancel()

    target = tmp_path / "file"
    with cdn({"/file": data}) as s:
        with pytest.raises(Cancelled):
            dl.download(
                s["url"] + "/file",
                target,
                expected_size=len(data),
                range_workers=4,
                cancel=cancel,
                emit=stop,
            )
        # Retain an old layout's completed span above the new worker count.
        (tmp_path / "file.part-3.done").write_bytes(b"old layout")
        dl.download(s["url"] + "/file", target, expected_size=len(data), range_workers=2)
    assert target.read_bytes() == data
    assert not list(tmp_path.glob("file.part-*"))


def test_summary_log_is_outside_the_install_tree(tmp_path):
    from nadare.core import logging as nadare_logging
    from nadare.core.transfer import measure

    with measure(2, None):
        pass
    log = nadare_logging.log_path()
    assert not log.is_relative_to(tmp_path)
    assert "Download sync: workers requested=2" in log.read_text()


@pytest.mark.parametrize("failure", ["mkdir", "open", "lock", "corrupt", "write"])
def test_unusable_shared_budget_falls_back_to_local_limit(tmp_path, monkeypatch, failure):
    import errno

    from nadare.core import transfer

    bucket = transfer.TokenBucket()
    path = tmp_path / "config" / "budget"

    def denied(*args, **kwargs):
        raise OSError(errno.EROFS, "fixture: read-only configuration")

    # Flatpak may expose the config but refuse a separate budget file or flock.
    monkeypatch.setenv("FLATPAK_ID", "io.test.Nadare")
    if failure == "mkdir":
        monkeypatch.setattr(type(path), "mkdir", denied)
    bucket.attach(path)
    if failure == "open":
        monkeypatch.setattr(transfer.os, "open", denied)
    elif failure == "lock":
        def locked(*args):
            assert args[1] & transfer.fcntl.LOCK_NB
            raise BlockingIOError(errno.EWOULDBLOCK, "fixture: another instance holds lock")
        monkeypatch.setattr(transfer.fcntl, "flock", locked)
    elif failure == "write":
        monkeypatch.setattr(transfer.json, "dump", denied)
    bucket.set_limit(0.1)
    if failure == "corrupt":
        path.write_text('["bad", 0, 0]')
    started = time.monotonic()
    bucket.take(20000)
    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 2, elapsed
    bucket.set_limit(0)
    bucket.take(20000)


def test_two_starting_instances_preserve_shared_budget(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "budget"
    script = (
        "from nadare.core.transfer import TokenBucket; "
        f"b=TokenBucket(); b.attach({str(path)!r}); b.set_limit(1); b.take(1000000)"
    )
    started = time.monotonic()
    children = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(2)]
    try:
        for child in children:
            child.wait(timeout=5)
            assert child.returncode == 0
        assert time.monotonic() - started >= 1.8
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize("raw", ['["bad", 0, 0]', '[1, -1, 0]', '[1, 0, NaN]', b'\xff'])
def test_damaged_budget_during_download_retains_local_rate(tmp_path, raw):
    from nadare.core.transfer import TokenBucket

    bucket = TokenBucket()
    path = tmp_path / "budget"
    bucket.attach(path)
    bucket.set_limit(0.1)
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    started = time.monotonic()
    bucket.take(20000)
    assert 0.15 <= time.monotonic() - started < 2


def test_unlimited_child_sees_a_new_shared_limit(tmp_path):
    import subprocess
    import sys

    from nadare.core.transfer import TokenBucket

    path = tmp_path / "budget"
    bucket = TokenBucket()
    bucket.attach(path)
    bucket.set_limit(0)
    script = (
        "from nadare.core.transfer import TokenBucket; "
        f"b=TokenBucket(); b.attach({str(path)!r}); "
        "print('ready', flush=True); input(); b.take(200000)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            assert child.stdout.readline().strip() == "ready"
            bucket.set_limit(0.2)
            started = time.monotonic()
            child.stdin.write("go\n")
            child.stdin.flush()
            child.wait(timeout=3)
            assert child.returncode == 0
            assert 0.8 <= time.monotonic() - started < 3
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_background_news_cannot_contact_remote_host(monkeypatch):
    from curl_cffi import requests

    from nadare.core import news

    def forbidden(*args, **kwargs):
        pytest.fail("background news reached the real HTTP transport")

    monkeypatch.setattr(requests, "get", forbidden)
    with pytest.raises(news.NewsError, match="network is disabled in tests"):
        news.http_get_json("https://example.invalid/feed")
