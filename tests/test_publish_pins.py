# SPDX-License-Identifier: GPL-3.0-or-later
"""scripts/publish_pins.py: one node publishes block pins for a game (no network: the plan and
the byte stream are injected)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest

from nadare.core import archivepins, directdl

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_pins.py"
URL = "https://cdn.example/game.zip"
DATA = bytes((i * 11 + 5) % 253 for i in range(10_000))
MD5 = hashlib.md5(DATA).hexdigest()


@pytest.fixture(scope="module")
def pp():
    spec = importlib.util.spec_from_file_location("publish_pins_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_for(md5: str | None = MD5, size: int | None = None, url: str = URL):
    source = directdl.RemoteFile(path="game.zip", url=url, size=size, md5=md5)
    archive = directdl.Archive(name="game.zip", source=source, unwrap=("zip",), stream=True)
    return directdl.Plan(game_tag="game", version="1", archives=(archive,), exe="g.exe")


class Stream:
    """A fake byte stream that counts how often it was opened."""

    def __init__(self, data: bytes = DATA):
        self.data = data
        self.opened = 0

    def __call__(self, url, headers):
        self.opened += 1
        yield self.data[:3000]
        yield self.data[3000:]


def pin_path(index: Path) -> Path:
    return index / "catalog" / "archive-pins" / "game.json"


def test_publishes_a_pin_document_that_matches_the_plan(pp, tmp_path):
    stream = Stream()
    result = pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    assert result == "published"
    pins = archivepins.parse_document(json.loads(pin_path(tmp_path).read_text()))
    pin = pins["game.zip"]
    assert pin.matches(plan_for().archives[0].parts or [plan_for().archives[0].source], MD5)
    assert pin.parts[0].size == len(DATA)
    assert list(pin.parts[0].blocks) == archivepins.hash_blocks(DATA, 4096)


def test_unchanged_archive_is_not_downloaded_again(pp, tmp_path):
    stream = Stream()
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    before = pin_path(tmp_path).read_bytes()
    again = pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    assert again == "unchanged"
    assert stream.opened == 1
    assert pin_path(tmp_path).read_bytes() == before


def test_new_version_replaces_the_pin(pp, tmp_path):
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    other = DATA[::-1]
    result = pp.publish_pin(
        tmp_path,
        "game",
        resolve=lambda: plan_for(md5=hashlib.md5(other).hexdigest(), url=URL + "?v=2"),
        open_stream=Stream(other),
        block_size=4096,
    )
    assert result == "published"
    pins = archivepins.parse_document(json.loads(pin_path(tmp_path).read_text()))
    assert pins["game.zip"].parts[0].url == URL + "?v=2"


def test_md5_mismatch_keeps_the_old_pin_and_fails(pp, tmp_path):
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    before = pin_path(tmp_path).read_bytes()
    with pytest.raises(pp.PublishPinError, match="md5"):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=lambda: plan_for(md5="f" * 32, url=URL + "?v=2"),
            open_stream=Stream(),
            block_size=4096,
        )
    assert pin_path(tmp_path).read_bytes() == before


def test_a_plan_without_a_publisher_md5_is_refused(pp, tmp_path):
    with pytest.raises(pp.PublishPinError, match="md5"):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=lambda: plan_for(md5=None),
            open_stream=Stream(),
            block_size=4096,
        )
    assert not pin_path(tmp_path).exists()


def test_a_held_lock_skips_the_game_without_downloading(pp, tmp_path):
    stream = Stream()
    with pp.game_lock(tmp_path, "game"):
        with pytest.raises(pp.PublishPinError, match="lock"):
            pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    assert stream.opened == 0


def test_unknown_game_tag_is_an_error(pp, tmp_path):
    with pytest.raises(pp.PublishPinError, match="unknown game"):
        pp.resolver_for("no-such-game")


def test_game_tag_is_validated_before_it_reaches_a_path(pp, tmp_path):
    with pytest.raises(pp.PublishPinError):
        pp.publish_pin(tmp_path, "../evil", resolve=plan_for, open_stream=Stream())


class Endless:
    """A stream that never ends; counts the bytes handed out."""

    def __init__(self, chunk: int = 1000, delay: float = 0.0):
        self.chunk = chunk
        self.delay = delay
        self.sent = 0

    def __call__(self, url, headers):
        while True:
            if self.delay:
                time.sleep(self.delay)
            self.sent += self.chunk
            yield b"x" * self.chunk


def test_archives_not_in_the_plan_are_pruned(pp, tmp_path):
    path = pin_path(tmp_path)
    path.parent.mkdir(parents=True)
    stale = archivepins.pin_from_bytes("https://cdn.example/old.zip", b"x" * 5000, 4096)
    path.write_text(json.dumps(archivepins.document({"old.zip": stale})))
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    pins = archivepins.parse_document(json.loads(path.read_text()))
    assert set(pins) == {"game.zip"}


def test_a_matching_pin_still_prunes_stale_entries_without_downloading(pp, tmp_path):
    stream = Stream()
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    doc = json.loads(pin_path(tmp_path).read_text())
    stale = archivepins.pin_from_bytes("https://cdn.example/old.zip", b"x" * 5000, 4096)
    doc["archives"]["old.zip"] = archivepins.document({"old.zip": stale})["archives"]["old.zip"]
    pin_path(tmp_path).write_text(json.dumps(doc))
    result = pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    assert result == "published"
    assert stream.opened == 1
    assert set(json.loads(pin_path(tmp_path).read_text())["archives"]) == {"game.zip"}


def test_a_damaged_existing_document_is_regenerated(pp, tmp_path):
    path = pin_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 1, "archives": {"old.zip": ')
    result = pp.publish_pin(
        tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096
    )
    assert result == "published"
    assert set(archivepins.parse_document(json.loads(path.read_text()))) == {"game.zip"}


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe not utf-8 \x80", b"[" * 100_000],
    ids=["invalid-utf8", "deeply-nested"],
)
def test_an_undecodable_existing_document_is_regenerated(pp, tmp_path, raw):
    path = pin_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    result = pp.publish_pin(
        tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096
    )
    assert result == "published"
    assert set(archivepins.parse_document(json.loads(path.read_text()))) == {"game.zip"}


def test_a_document_over_the_client_cap_is_refused(pp, tmp_path, monkeypatch):
    monkeypatch.setattr(archivepins, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(pp.PublishPinError, match="too large"):
        pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    assert not pin_path(tmp_path).exists()


def test_a_url_the_client_would_reject_is_refused_before_downloading(pp, tmp_path):
    stream = Stream()
    with pytest.raises(pp.PublishPinError, match="url"):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=lambda: plan_for(url="http://cdn.example/game.zip"),
            open_stream=stream,
            block_size=4096,
        )
    assert stream.opened == 0
    assert not pin_path(tmp_path).exists()


def test_a_changed_block_size_republishes(pp, tmp_path):
    stream = Stream()
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    result = pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=8192)
    assert result == "published"
    assert stream.opened == 2
    assert (
        archivepins.parse_document(json.loads(pin_path(tmp_path).read_text()))[
            "game.zip"
        ].block_size
        == 8192
    )


def test_an_existing_pin_without_a_publisher_md5_is_rebuilt(pp, tmp_path):
    path = pin_path(tmp_path)
    path.parent.mkdir(parents=True)
    legacy = archivepins.pin_from_bytes(URL, DATA, 4096)
    path.write_text(json.dumps(archivepins.document({"game.zip": legacy})))
    stream = Stream()
    result = pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    assert result == "published"
    assert stream.opened == 1
    assert archivepins.parse_document(json.loads(path.read_text()))["game.zip"].publisher_md5 == MD5


def test_an_empty_plan_is_an_error(pp, tmp_path):
    empty = directdl.Plan(game_tag="game", version="1", archives=(), exe="g.exe")
    with pytest.raises(pp.PublishPinError, match="no archives"):
        pp.publish_pin(tmp_path, "game", resolve=lambda: empty, open_stream=Stream())


def test_an_invalid_block_size_fails_even_when_nothing_changed(pp, tmp_path):
    stream = Stream()
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    with pytest.raises(pp.PublishPinError, match="block size"):
        pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=12345)


def test_a_stalled_stream_hits_the_deadline_and_writes_nothing(pp, tmp_path):
    started = time.monotonic()
    with pytest.raises(pp.PublishPinError, match="deadline"):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=plan_for,
            open_stream=Endless(delay=0.2),
            block_size=4096,
            deadline=1,
        )
    assert time.monotonic() - started < 4
    assert not pin_path(tmp_path).exists()


def test_an_outer_alarm_timer_and_handler_survive_a_run(pp, tmp_path):
    import signal

    def sentinel(signum, frame):
        pass

    old = signal.signal(signal.SIGALRM, sentinel)
    signal.setitimer(signal.ITIMER_REAL, 100)
    try:
        pp.publish_pin(
            tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096, deadline=30
        )
        assert signal.getsignal(signal.SIGALRM) is sentinel
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 100
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_no_alarm_is_left_behind_when_there_was_none(pp, tmp_path):
    import signal

    signal.setitimer(signal.ITIMER_REAL, 0)
    pp.publish_pin(
        tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096, deadline=30
    )
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_a_deadline_that_expires_during_the_write_does_not_fail_the_publish(
    pp, tmp_path, monkeypatch
):
    real = pp._atomic_write_bytes

    def slow(path, data):
        time.sleep(1.5)
        real(path, data)

    monkeypatch.setattr(pp, "_atomic_write_bytes", slow)
    result = pp.publish_pin(
        tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096, deadline=1
    )
    assert result == "published"
    assert pin_path(tmp_path).exists()


def test_an_endless_stream_stops_at_the_byte_cap(pp, tmp_path):
    stream = Endless(chunk=1000)
    with pytest.raises(pp.PublishPinError, match="limit"):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=plan_for,
            open_stream=stream,
            block_size=4096,
            max_bytes=20_000,
        )
    assert stream.sent <= 22_000
    assert not pin_path(tmp_path).exists()


def test_the_published_file_is_world_readable(pp, tmp_path):
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    assert pin_path(tmp_path).stat().st_mode & 0o044 == 0o044


def test_the_published_document_passes_the_client_validation(pp, tmp_path):
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    assert archivepins._is_valid_document(json.loads(pin_path(tmp_path).read_text()))


def test_main_exit_codes(pp, tmp_path, capsys):
    stream = Stream()
    argv = ["--index", str(tmp_path), "--game", "game", "--block-size", "4096"]
    assert pp.main(argv, resolve=plan_for, open_stream=stream) == 0
    assert pp.main(argv, resolve=plan_for, open_stream=stream) == pp.UNCHANGED
    bad = lambda: plan_for(md5="f" * 32, url=URL + "?v=2")  # noqa: E731
    assert pp.main(argv, resolve=bad, open_stream=Stream()) == 1
    assert "FAILED" in capsys.readouterr().out
    assert pp.main([*argv[:-1], "12345"], resolve=plan_for, open_stream=stream) == 1


def test_a_held_lock_is_skipped_not_failed(pp, tmp_path, capsys):
    stream = Stream()
    argv = ["--index", str(tmp_path), "--game", "game", "--block-size", "4096"]
    with pp.game_lock(tmp_path, "game"):
        assert pp.main(argv, resolve=plan_for, open_stream=stream) == pp.UNCHANGED
    assert "lock" in capsys.readouterr().out
    assert stream.opened == 0


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self.body = body
        self.closed = False
        self._pos = 0

    def read(self, amount: int = -1) -> bytes:
        chunk = self.body[self._pos : self._pos + amount]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _fake_pool(response):
    class Pool:
        instances: list = []

        def __init__(self, *a, **k):
            self.closed = False
            Pool.instances.append(self)

        def open(self, url, *, headers=None, **kw):
            return response

        def close(self):
            self.closed = True

    return Pool


@pytest.mark.parametrize("status", [206, 301, 403, 500])
def test_open_stream_refuses_anything_but_200_and_closes(pp, monkeypatch, status):
    from nadare.core import download

    response = _FakeResponse(status, b"data")
    pool = _fake_pool(response)
    monkeypatch.setattr(download, "HttpClientPool", pool)
    with pytest.raises(pp.PublishPinError, match=str(status)):
        list(pp.open_stream(URL, {}))
    assert response.closed
    assert all(p.closed for p in pool.instances)


def test_open_stream_yields_the_body_and_closes(pp, monkeypatch):
    from nadare.core import download

    response = _FakeResponse(200, b"a" * 2_500_000)
    pool = _fake_pool(response)
    monkeypatch.setattr(download, "HttpClientPool", pool)
    assert b"".join(pp.open_stream(URL, {})) == b"a" * 2_500_000
    assert response.closed
    assert all(p.closed for p in pool.instances)


def test_a_built_document_that_does_not_validate_is_refused(pp, tmp_path, monkeypatch):
    def reject(doc):
        raise archivepins.PinError("boom")

    monkeypatch.setattr(archivepins, "parse_document", reject)
    with pytest.raises(pp.PublishPinError, match="does not validate"):
        pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    assert not pin_path(tmp_path).exists()


@pytest.mark.parametrize(
    "bad",
    [{"md5": 12345}, {"url": None}, {"url": 12345}],
    ids=["md5-int", "url-none", "url-int"],
)
def test_wrong_typed_plan_data_is_a_publish_error(pp, tmp_path, bad):
    stream = Stream()
    with pytest.raises(pp.PublishPinError):
        pp.publish_pin(
            tmp_path,
            "game",
            resolve=lambda: plan_for(**bad),
            open_stream=stream,
            block_size=4096,
        )
    assert stream.opened == 0
    assert not pin_path(tmp_path).exists()


def test_an_unwritable_pin_directory_is_found_before_streaming(pp, tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    pin_dir = pin_path(tmp_path).parent
    pin_dir.mkdir(parents=True)
    pin_dir.chmod(0o500)
    stream = Stream()
    try:
        with pytest.raises(pp.PublishPinError, match="writable"):
            pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=stream, block_size=4096)
    finally:
        pin_dir.chmod(0o755)
    assert stream.opened == 0


def test_an_unreadable_existing_document_is_an_error_not_a_reason_to_overwrite(pp, tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    pp.publish_pin(tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096)
    path = pin_path(tmp_path)
    before = path.read_bytes()
    path.chmod(0o000)
    try:
        with pytest.raises(pp.PublishPinError, match="cannot read"):
            pp.publish_pin(
                tmp_path, "game", resolve=plan_for, open_stream=Stream(), block_size=4096
            )
    finally:
        path.chmod(0o644)
    assert path.read_bytes() == before


def test_the_deadline_covers_the_whole_run_not_each_game(pp, tmp_path, capsys):
    started = time.monotonic()
    rc = pp.main(
        [
            "--index",
            str(tmp_path),
            "--game",
            "game",
            "--game",
            "other",
            "--block-size",
            "4096",
            "--deadline",
            "1",
        ],
        resolve=plan_for,
        open_stream=Endless(delay=0.2),
    )
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("FAILED") == 2
    assert "deadline" in out
    assert elapsed < 1.7
