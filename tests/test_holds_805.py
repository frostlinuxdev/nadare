# SPDX-License-Identifier: GPL-3.0-or-later
"""#805: a signed hold list can stop Play for a game the maintainers marked dangerous.

The feed is signed with the release key, like ``runners.json``. Holds only ever refuse a
launch: install, update, repair and stop keep working, and a network problem never blocks.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_release_signature import sign

from nadare import app
from nadare.core import (
    atomicfile,
    graphics,
    holds,
    hostlibs,
    indexfetch,
    launchgate,
    paths,
    process,
    releasesig,
    runners,
)
from nadare.core import config as config_module
from nadare.core.config import Config

URL = "https://fixture.invalid/index/holds.json"


def _hold(**over) -> dict:
    row = {
        "id": "nikke-2026-11",
        "kinds": ["launcher-nikke"],
        "below": None,
        "reason": "Reported compromise of the publisher's launcher.",
        "link": "https://github.com/frostlinuxdev/nadare/issues/805",
    }
    row.update(over)
    return row


def _doc(*rows: dict, version: int = 1, serial: int = 1) -> dict:
    return {"version": version, "serial": serial, "holds": list(rows)}


@pytest.fixture
def feed(monkeypatch, tmp_path):
    """A fake index: ``state["doc"]`` is what the opener serves, signed by a test key."""
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: verify(p, s, key=pinned))
    state = {"doc": _doc(_hold()), "body_doc": None, "signed_doc": None, "error": None,
             "bad_signature": False, "calls": [], "raw_body": None}

    def opener(url: str) -> bytes:
        state["calls"].append(url)
        if state["error"]:
            raise state["error"]
        body_doc = state["body_doc"] if state["body_doc"] is not None else state["doc"]
        signed_doc = state["signed_doc"] if state["signed_doc"] is not None else body_doc
        body = state["raw_body"] if state["raw_body"] is not None else json.dumps(body_doc).encode()
        signed = (
            state["raw_body"]
            if state["raw_body"] is not None
            else json.dumps(signed_doc).encode()
        )
        if state["bad_signature"]:
            signed = b"not the document that was signed"
        envelope = {"body": body.decode(), "signature": sign(signed, key, fingerprint)}
        return json.dumps(envelope).encode()

    return state, opener


# ------------------------------------------------------------------ parsing
def test_a_minimal_hold_is_accepted():
    [item] = holds.parse(_doc(_hold()), url=URL)
    assert item.id == "nikke-2026-11"
    assert item.kinds == ("launcher-nikke",)
    assert item.below is None
    assert item.reason and item.link


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "x",
        {"version": 1, "holds": "x"},
        {"version": 1, "holds": [None, 5, "s", {}]},
        _doc(_hold(id="")),
        _doc(_hold(id="a b")),
        _doc(_hold(id="x" * 65)),
        _doc(_hold(kinds=[])),
        _doc(_hold(kinds="launcher-nikke")),
        _doc(_hold(kinds=["launcher nikke"])),
        _doc(_hold(below="not-a-version")),
        _doc(_hold(reason="")),
        _doc(_hold(reason="   ")),
    ],
)
def test_a_malformed_hold_is_skipped(bad):
    assert holds.parse(bad, url=URL) == []


def test_one_bad_entry_does_not_drop_the_rest():
    items = holds.parse(_doc(_hold(id="bad", below="nonsense"), _hold(id="good")), url=URL)
    assert [item.id for item in items] == ["good"]


def test_at_most_twenty_holds():
    items = holds.parse(_doc(*[_hold(id=f"h{i}") for i in range(30)]), url=URL)
    assert len(items) == 20
    assert [item.id for item in items] == [f"h{i}" for i in range(20)]


def test_duplicate_ids_collapse():
    items = holds.parse(
        _doc(_hold(id="same", reason="first"), _hold(id="same", reason="second")), url=URL
    )
    assert [item.reason for item in items] == ["first"]


@pytest.mark.parametrize(
    "link",
    [
        "http://github.com/frostlinuxdev/nadare",
        "https://evil.example/x",
        "https://github.com.evil.example/x",
        "https://user:pw@github.com/x",
        "https://github.com:8443/x",
        "javascript:alert(1)",
        "",
        5,
    ],
)
def test_a_link_off_the_allowlist_is_dropped_but_the_hold_stays(link):
    [item] = holds.parse(_doc(_hold(link=link)), url=URL)
    assert item.link is None and item.reason


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/frostlinuxdev/nadare/issues/805",
        "https://frostlinuxdev.github.io/",
        "https://fixture.invalid/page",  # the host the feed came from
    ],
)
def test_allowlisted_links_are_kept(link):
    [item] = holds.parse(_doc(_hold(link=link)), url=URL)
    assert item.link == link


# ---------------------------------------------------------------- applicability
def test_a_hold_applies_to_its_kind_only():
    [item] = holds.parse(_doc(_hold(kinds=["launcher-nikke"])), url=URL)
    assert holds.applies(item, "launcher-nikke", "0.1.0")
    assert not holds.applies(item, "wine", "0.1.0")
    assert not holds.applies(item, "stove-czn", "0.1.0")


def test_below_targets_older_app_versions_only():
    [item] = holds.parse(_doc(_hold(below="0.2.0")), url=URL)
    assert holds.applies(item, "launcher-nikke", "0.1.9")
    assert not holds.applies(item, "launcher-nikke", "0.2.0")
    assert not holds.applies(item, "launcher-nikke", "0.2.1")
    assert not holds.applies(item, "launcher-nikke", "not a version")


def test_an_override_skips_only_that_hold_id(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="launcher-nikke")
    first = holds.parse(_doc(_hold(id="first")), url=URL)[0]
    second = holds.parse(_doc(_hold(id="second")), url=URL)[0]

    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[first, second]) is first
    config.profile("g")["hold_override"] = "first"
    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[first, second]) is second
    config.profile("g")["hold_override"] = "second"
    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[first, second]) is first


def test_every_accepted_id_of_a_backend_is_remembered(tmp_path):
    """Two holds may cover one backend: accepting one must not leave the other blocking."""
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="launcher-nikke")
    first = holds.parse(_doc(_hold(id="first")), url=URL)[0]
    second = holds.parse(_doc(_hold(id="second")), url=URL)[0]
    third = holds.parse(_doc(_hold(id="third")), url=URL)[0]

    config.profile("g")["hold_override"] = ["first", "second"]
    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[first, second]) is None
    # A newly published id still asks, even though the older ones are acknowledged.
    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[first, second, third]) is third


def test_acknowledging_a_hold_keeps_the_older_ones_and_the_string_form():
    assert holds.acknowledge({"hold_override": "first"}, "second") == ["first", "second"]
    assert holds.acknowledge({"hold_override": ["first"]}, "second") == ["first", "second"]
    assert holds.acknowledge({"hold_override": ["first", "second"]}, "second") == [
        "first",
        "second",
    ]
    assert holds.acknowledge({}, "first") == ["first"]


def test_the_switch_off_disables_every_hold(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="launcher-nikke")
    item = holds.parse(_doc(_hold()), url=URL)[0]
    config.data["check_holds"] = False
    assert holds.blocking_hold(config, "g", "launcher-nikke", holds=[item]) is None


def test_holds_are_on_by_default():
    assert config_module.TOP_DEFAULTS["check_holds"] is True


# ------------------------------------------------------------------ signature + cache
def test_a_good_signed_feed_is_used(feed):
    state, opener = feed
    items = holds.load(opener=opener)
    assert [item.id for item in items] == ["nikke-2026-11"]
    assert state["calls"] == [URL]


def test_a_bad_signature_is_ignored(feed):
    state, opener = feed
    assert holds.load(opener=opener)  # the good copy is cached
    state["bad_signature"] = True
    state["doc"] = _doc(_hold(id="tampered"))
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    assert holds.last_problem() is not None


def test_a_body_changed_after_signing_is_ignored(feed):
    state, opener = feed
    assert holds.load(opener=opener)
    state["body_doc"] = _doc(_hold(id="changed-after-signing"))
    state["signed_doc"] = _doc(_hold())
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]


def test_a_network_failure_fails_open_and_keeps_the_last_verified_list(feed):
    state, opener = feed
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    state["error"] = OSError("offline")
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    assert len(state["calls"]) == 2


def test_a_first_failure_blocks_nothing(feed):
    state, opener = feed
    state["error"] = OSError("offline")
    assert holds.load(opener=opener) == []


def test_a_corrupt_document_is_ignored(feed):
    state, opener = feed
    state["doc"] = {"version": 99, "holds": [_hold()]}
    assert holds.load(opener=opener) == []


def test_a_bad_signature_gives_one_quiet_system_check_line(feed, monkeypatch):
    state, opener = feed
    state["bad_signature"] = True
    assert holds.load(opener=opener) == []
    assert holds.last_problem() is not None
    monkeypatch.setattr(holds, "load", lambda **k: [])
    assert holds.system_check_line() == f"holds: {holds.last_problem()}"


def test_the_system_check_prints_the_hold_line(tmp_path, monkeypatch):
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(hostlibs, "distro_family", lambda: "debian")
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(holds, "system_check_line", lambda: "holds: test line")
    out = app._doctor_text(Config.load(tmp_path / "config.json"))
    assert "holds: test line" in out


def test_a_bad_document_is_ignored_before_its_body_is_parsed(feed, monkeypatch):
    state, opener = feed
    state["bad_signature"] = True

    class _NoJson:
        @staticmethod
        def loads(*_a, **_k):
            raise AssertionError("the unsigned body must never be parsed")

    monkeypatch.setattr(holds, "json", _NoJson)
    assert holds.load(opener=opener) == []


# ------------------------------------------------------------------ anti-rollback
def _state_record() -> dict:
    return json.loads((paths.state_dir() / "holds-serial.json").read_text())


def test_the_accepted_serial_body_hash_and_envelope_are_saved(feed):
    state, opener = feed
    state["doc"] = _doc(_hold(), serial=42)  # no saved state yet: any valid serial is accepted
    assert holds.load(opener=opener)
    body = json.dumps(state["doc"]).encode()
    record = _state_record()
    assert record["serial"] == 42
    assert record["sha256"] == hashlib.sha256(body).hexdigest()
    # The accepted signed body is saved with the serial, so it survives a failed cache write.
    assert record["body"].encode() == body
    assert record["signature"]


def test_the_saved_hash_is_of_the_exact_signed_bytes(feed):
    state, opener = feed
    # Deliberately not json.dumps() layout: the watermark must hash the bytes that were signed.
    state["raw_body"] = (
        b'{"version": 1,   "holds": [{"id": "nikke-2026-11", '
        b'"kinds": ["launcher-nikke"], "below": null, "reason": "x", "link": null}], '
        b'"serial": 7}'
    )
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    assert _state_record()["serial"] == 7
    assert _state_record()["sha256"] == hashlib.sha256(state["raw_body"]).hexdigest()


def test_a_newer_serial_replaces_the_saved_one(feed):
    state, opener = feed
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    state["doc"] = _doc(_hold(id="newer"), serial=2)
    assert [item.id for item in holds.load(opener=opener)] == ["newer"]
    assert _state_record()["serial"] == 2


def test_an_older_serial_is_refused_and_the_newer_holds_stay(feed):
    state, opener = feed
    state["doc"] = _doc(_hold(id="newer"), serial=2)
    assert [item.id for item in holds.load(opener=opener)] == ["newer"]
    # A mirror replays the older, validly signed list to switch the newer hold off.
    state["doc"] = _doc(_hold(id="older"), serial=1)
    assert [item.id for item in holds.load(opener=opener)] == ["newer"]
    assert holds.last_problem() is not None


def test_the_same_serial_with_a_different_body_is_refused(feed):
    state, opener = feed
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    state["doc"] = _doc(_hold(id="swapped"), serial=1)
    assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    assert holds.last_problem() is not None


def test_the_same_serial_with_the_same_body_is_accepted_again(feed):
    state, opener = feed
    first = holds.load(opener=opener)
    assert [item.id for item in holds.load(opener=opener)] == [item.id for item in first]


@pytest.mark.parametrize("serial", [None, 0, -1, "1", 1.5, True])
def test_a_document_without_a_usable_serial_is_refused(feed, serial):
    state, opener = feed
    state["doc"] = (
        {"version": 1, "holds": [_hold()]}
        if serial is None
        else _doc(_hold(), serial=serial)
    )
    assert holds.load(opener=opener) == []
    assert holds.last_problem() is not None


def test_known_prefers_the_memo(monkeypatch):
    item = holds.parse(_doc(_hold()), url=URL)[0]
    monkeypatch.setattr(holds, "_MEMO", (time.time(), [item], 3600))
    monkeypatch.setattr(
        indexfetch, "read_cached", lambda rel: pytest.fail("the cache must not be read")
    )
    assert holds.known() == [item]


def test_known_answers_from_the_cache_without_fetching(feed, monkeypatch):
    state, opener = feed
    assert holds.load(opener=opener)
    monkeypatch.setattr(holds, "_MEMO", None)
    monkeypatch.setattr(
        indexfetch,
        "fetch_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("known() must not fetch")),
    )
    assert [item.id for item in holds.known()] == ["nikke-2026-11"]


def test_known_falls_back_to_the_accepted_copy_when_the_cache_breaks(feed, monkeypatch):
    state, opener = feed
    assert holds.load(opener=opener)
    monkeypatch.setattr(holds, "_MEMO", None)
    path = indexfetch.cache_path("https://fixture.invalid/index/", holds.CACHE_REL)
    envelope = json.loads(path.read_text())
    envelope["signature"] = "broken"
    path.write_text(json.dumps(envelope))
    # The saved accepted body is signed and verified again, so the hold is still known.
    assert [item.id for item in holds.known()] == ["nikke-2026-11"]


def test_known_is_empty_when_no_saved_copy_can_be_trusted(feed, monkeypatch):
    state, opener = feed
    monkeypatch.setattr(holds, "_MEMO", None)
    envelope = json.loads(opener(URL))
    cache = indexfetch.cache_path("https://fixture.invalid/index/", holds.CACHE_REL)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(dict(envelope, signature="broken")))
    # The saved record is verified again too: a body that its signature does not cover is
    # not used, even though the record's own hash matches it.
    payload = json.dumps(_doc(_hold(id="tampered"))).encode()
    record = holds.serial_path()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "serial": 1,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "body": payload.decode(),
                "signature": envelope["signature"],
            }
        )
    )
    assert holds.known() == []
    # Nor is a record whose stated serial does not match the body that it saved.
    state["doc"] = _doc(_hold(id="mismatch"), serial=1)
    signed = json.loads(opener(URL))
    record.write_text(
        json.dumps(
            {
                "serial": 99,
                "sha256": hashlib.sha256(signed["body"].encode()).hexdigest(),
                "body": signed["body"],
                "signature": signed["signature"],
            }
        )
    )
    assert holds.known() == []
    state["error"] = OSError("offline")
    assert holds.load(opener=opener) == []


def test_a_corrupt_serial_file_is_treated_as_missing_and_logged(feed, caplog):
    state, opener = feed
    path = paths.state_dir() / "holds-serial.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with caplog.at_level("WARNING", logger="nadare.core.holds"):
        assert [item.id for item in holds.load(opener=opener)] == ["nikke-2026-11"]
    assert "serial" in caplog.text.lower()


# ------------------------------------------------- durability and concurrency
def _fail_cache_writes(monkeypatch) -> None:
    """Make the index cache copy of holds.json unwritable, as a full disk would.

    ``indexfetch`` logs a failed cache write and carries on, so the accepted document is
    only in the state record afterwards. The state record itself still writes.
    """
    import_write = atomicfile.write

    def write(path, data, **kw):
        if Path(path).name == holds.CACHE_REL:
            raise OSError("disk full")
        return import_write(path, data, **kw)

    monkeypatch.setattr(atomicfile, "write", write)


def test_a_failed_cache_write_keeps_the_accepted_hold_offline(feed, monkeypatch):
    state, opener = feed
    state["doc"] = _doc(_hold(id="older-hold"), serial=1)
    first_body = json.dumps(state["doc"]).encode()
    assert [item.id for item in holds.load(opener=opener)] == ["older-hold"]

    _fail_cache_writes(monkeypatch)
    state["doc"] = _doc(_hold(id="new-hold"), serial=2)
    assert [item.id for item in holds.load(opener=opener)] == ["new-hold"]
    assert _state_record()["serial"] == 2
    cache = indexfetch.cache_path("https://fixture.invalid/index/", holds.CACHE_REL)
    assert json.loads(cache.read_text())["body"].encode() == first_body  # the cache stayed old

    # Next start: offline, the cache is too old to be a replay, but the new hold must survive.
    state["error"] = OSError("offline")
    assert [item.id for item in holds.load(opener=opener)] == ["new-hold"]
    assert "last good copy" in holds.last_problem()


def test_known_uses_the_accepted_copy_when_the_cache_is_stale(feed, monkeypatch):
    state, opener = feed
    state["doc"] = _doc(_hold(id="older-hold"), serial=1)
    assert holds.load(opener=opener)
    _fail_cache_writes(monkeypatch)
    state["doc"] = _doc(_hold(id="new-hold"), serial=2)
    assert [item.id for item in holds.load(opener=opener)] == ["new-hold"]

    monkeypatch.setattr(holds, "_MEMO", None)
    assert [item.id for item in holds.known()] == ["new-hold"]


def test_concurrent_accept_cannot_lower_the_watermark(feed, monkeypatch):
    state, opener = feed
    state["doc"] = _doc(_hold(id="first"), serial=1)
    assert holds.load(opener=opener)

    def envelope_for(document):
        state["doc"] = document
        return json.loads(opener(URL))

    older = envelope_for(_doc(_hold(id="two"), serial=2))
    newer = envelope_for(_doc(_hold(id="three"), serial=3))

    real_write = atomicfile.write
    at_the_write = threading.Event()
    let_it_write = threading.Event()

    def write(path, data, **kw):
        if Path(path).name == holds.SERIAL_FILE:
            try:
                record = json.loads(data)
            except ValueError:
                record = None
            if isinstance(record, dict) and record.get("serial") == 2:
                at_the_write.set()
                # A correct lock makes the other acceptance wait for this write.
                let_it_write.wait(5)
        return real_write(path, data, **kw)

    monkeypatch.setattr(atomicfile, "write", write)
    errors: list[BaseException] = []

    def accept(envelope):
        try:
            holds._accept(envelope)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    second = threading.Thread(target=accept, args=(older,))
    second.start()
    try:
        assert at_the_write.wait(5), "the older acceptance never reached its write"
        third = threading.Thread(target=accept, args=(newer,))
        third.start()
        third.join(1.0)  # it must not finish while the older one is mid-transaction
        let_it_write.set()
        second.join(5)
        third.join(5)
    finally:
        let_it_write.set()
    assert errors == []
    assert _state_record()["serial"] == 3


def test_the_watermark_is_read_again_under_the_lock(feed, monkeypatch):
    """A caller that waited before its transaction must compare against the newest serial."""
    state, opener = feed
    state["doc"] = _doc(_hold(id="first"), serial=1)
    assert holds.load(opener=opener)

    def envelope_for(document):
        state["doc"] = document
        return json.loads(opener(URL))

    older = envelope_for(_doc(_hold(id="two"), serial=2))
    newer = envelope_for(_doc(_hold(id="three"), serial=3))

    real_lock = holds._state_lock
    waiting = threading.Event()
    let_go = threading.Event()

    @contextlib.contextmanager
    def slow_lock():
        if threading.current_thread().name == "older":
            waiting.set()
            let_go.wait(5)
        with real_lock():
            yield

    monkeypatch.setattr(holds, "_state_lock", slow_lock)
    results: dict[str, BaseException | None] = {}

    def accept(name, envelope):
        try:
            holds._accept(envelope)
            results[name] = None
        except BaseException as exc:  # noqa: BLE001 - asserted below
            results[name] = exc

    older_thread = threading.Thread(target=accept, args=("older", older), name="older")
    older_thread.start()
    assert waiting.wait(5)
    newer_thread = threading.Thread(target=accept, args=("newer", newer), name="newer")
    newer_thread.start()
    newer_thread.join(5)
    assert results.get("newer") is None, "the newer serial should have been accepted"
    let_go.set()
    older_thread.join(5)
    assert isinstance(results.get("older"), ValueError)
    assert _state_record()["serial"] == 3


def test_the_state_lock_is_exclusive_across_processes(feed):
    state, opener = feed
    state["doc"] = _doc(_hold(id="first"), serial=1)
    assert holds.load(opener=opener)
    state["doc"] = _doc(_hold(id="two"), serial=2)
    envelope = json.loads(opener(URL))

    holder = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", holder, str(holds.serial_path().with_suffix(".lock"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    finished = threading.Event()

    def accept():
        holds._accept(envelope)
        finished.set()

    worker = threading.Thread(target=accept)
    worker.daemon = True
    try:
        assert child.stdout is not None and child.stdin is not None
        assert child.stdout.readline().strip() == "locked"
        worker.start()
        assert finished.wait(0.3) is False, "the acceptance ignored the other process's lock"
    finally:
        child.stdin.write("go\n")
        child.stdin.flush()
        child.stdin.close()
        child.wait(10)
    assert finished.wait(10)
    worker.join(10)
    assert _state_record()["serial"] == 2


# ------------------------------------------------------------------ launch gate
class _Ctx:
    def __init__(self, config, name, profile):
        self.config = config
        self.name = name
        self.profile = profile


class _Backend:
    kind = "launcher-nikke"

    def __init__(self, config, name="g"):
        self.ctx = _Ctx(config, name, config.profile(name))
        self.log: list[str] = []

    def launch(self) -> int:
        self.log.append("launch")
        return 7


@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(launchgate.flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(launchgate.nvidiarestart, "guard_launch", lambda b: None)
    item = holds.parse(_doc(_hold()), url=URL)[0]
    monkeypatch.setattr(holds, "load", lambda **k: [item])
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="launcher-nikke")
    return config, item


def test_the_gate_refuses_a_held_kind_with_the_reason_and_link(gate):
    config, item = gate
    backend = _Backend(config)
    with pytest.raises(launchgate.HoldBlocked) as caught:
        launchgate.play(backend)
    assert caught.value.hold == item
    assert item.reason in str(caught.value)
    assert item.link in str(caught.value)
    assert backend.log == []


def test_the_gate_starts_after_the_switch_is_off(gate):
    config, _item = gate
    config.data["check_holds"] = False
    assert launchgate.play(_Backend(config)) == 7


def test_the_gate_starts_after_the_same_hold_was_accepted(gate):
    config, item = gate
    config.profile("g")["hold_override"] = item.id
    assert launchgate.play(_Backend(config)) == 7


def test_the_gate_starts_with_ignore_hold(gate):
    _config, _item = gate
    backend = _Backend(gate[0])
    assert launchgate.play(backend, ignore_hold=True) == 7


def test_a_hold_for_another_kind_does_not_block(gate):
    config, _item = gate
    backend = _Backend(config)
    backend.kind = "wine"
    assert launchgate.play(backend) == 7


# ------------------------------------------------------------------ CLI
def test_cli_launch_refuses_and_ignore_hold_starts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="launcher-nikke")
    config.save()
    item = holds.parse(_doc(_hold()), url=URL)[0]
    monkeypatch.setattr(holds, "load", lambda **k: [item])
    launched: list[str] = []

    class _CliBackend(_Backend):
        def launch(self) -> int:
            launched.append("launch")
            return 0

        def requires_notice_ack(self) -> bool:
            return False

    def create(name, config, **_kw):
        return _CliBackend(config, name)

    monkeypatch.setattr(app, "create_backend", create)
    monkeypatch.setattr(launchgate.flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(launchgate.nvidiarestart, "guard_launch", lambda b: None)

    assert app.run_cli(["-c", str(path), "launch", "-p", "g"]) == launchgate.HOLD_EXIT
    err = capsys.readouterr().err
    assert item.reason in err and item.link in err
    assert launched == []

    assert app.run_cli(["-c", str(path), "launch", "-p", "g", "--ignore-hold"]) == 0
    assert launched == ["launch"]


def test_the_cli_accepts_ignore_hold() -> None:
    parser = app.build_parser()
    assert parser.parse_args(["launch"]).ignore_hold is False
    assert parser.parse_args(["launch", "--ignore-hold"]).ignore_hold is True
