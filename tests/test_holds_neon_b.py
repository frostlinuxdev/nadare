# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression coverage for the verified round-B hold findings (#805)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_release_signature import sign

from nadare.core import (
    atomicfile,
    holds,
    indexfetch,
    notices,
    releasesig,
    updatecheck,
)
from nadare.core.config import Config

URL = "https://fixture.invalid/index/holds.json"

#: A profile dict can lack ``hold_override`` entirely; the rollback must restore that too.
_ABSENT = object()


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


def _signed_envelope(document: dict, key: Ed25519PrivateKey, fingerprint: bytes) -> dict:
    body = json.dumps(document).encode("utf-8")
    return {"body": body.decode("utf-8"), "signature": sign(body, key, fingerprint)}


@pytest.fixture
def signing_env(monkeypatch, tmp_path):
    """Isolated state/cache dirs and a fresh Ed25519 release key pinned into releasesig."""
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(holds, "_MEMO", None)
    monkeypatch.setattr(holds, "_LAST_PROBLEM", None)

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    real_verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: real_verify(p, s, key=pinned))
    return key, fingerprint, tmp_path


@pytest.mark.parametrize("corruption", ["serial", "digest", "signature"])
def test_unverified_serial_record_does_not_brick_valid_signed_holds(signing_env, corruption):
    """A saved record whose envelope does not verify must not act as the replay watermark.

    A corrupted or edited ``holds-serial.json`` may claim a serial the signed body does not
    carry. Treating it as the watermark makes every real feed look like a replay.
    """
    key, fingerprint, _tmp_path = signing_env

    saved = _signed_envelope(_doc(_hold(id="old"), serial=1), key, fingerprint)
    bad_record = {
        "serial": 99 if corruption == "serial" else 1,
        "sha256": hashlib.sha256(saved["body"].encode("utf-8")).hexdigest(),
        "body": saved["body"],
        "signature": saved["signature"],
    }
    if corruption == "digest":
        bad_record["sha256"] = "0" * 64
    elif corruption == "signature":
        bad_record["signature"] = "broken"
    atomicfile.write(holds.serial_path(), json.dumps(bad_record), make_parent=True, sync_dir=True)

    # The real feed carries the next signed serial and a hold that must block.
    incoming_serial = 2 if corruption == "serial" else 1
    payload = json.dumps(
        _signed_envelope(_doc(_hold(id="real-hold"), serial=incoming_serial), key, fingerprint)
    ).encode("utf-8")

    items = holds.load(opener=lambda _url: payload)
    assert [hold.id for hold in items] == ["real-hold"]


def test_concurrent_load_cannot_roll_memo_back_to_older_serial(signing_env, monkeypatch):
    """An older completion cannot replace a newer answer published in this process.

    Serializing the loads may legitimately make the second caller reuse the first answer.
    If the second caller fetches serial 2, however, serial 2 must remain the final memo.
    """
    key, fingerprint, _tmp_path = signing_env
    payloads = [
        json.dumps(_signed_envelope(_doc(serial=1), key, fingerprint)).encode(),
        json.dumps(_signed_envelope(_doc(_hold(), serial=2), key, fingerprint)).encode(),
    ]
    fetched = []

    def fetch(_url, **_kwargs):
        payload = payloads[len(fetched)]
        fetched.append(payload)
        return payload

    monkeypatch.setattr(indexfetch, "open_bytes", fetch)
    paused = threading.Event()
    second_done = threading.Event()
    real_write = indexfetch._write_cache

    def write(path: Path, payload: bytes):
        if threading.current_thread().name == "older-load":
            paused.set()
            # Wait for the newer load to finish, or for the load lock to time it out.
            second_done.wait(3)
        real_write(path, payload)

    monkeypatch.setattr(indexfetch, "_write_cache", write)
    results = {}
    errors = []

    def run(name):
        try:
            results[name] = holds.load()
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            if name == "newer":
                second_done.set()

    older = threading.Thread(target=run, args=("older",), name="older-load")
    newer = threading.Thread(target=run, args=("newer",), name="newer-load")
    older.start()
    try:
        assert paused.wait(10), "older load never reached cache write"
        newer.start()
        older.join(10)
        newer.join(10)
    finally:
        second_done.set()
        older.join(10)
        newer.join(10)
    assert not older.is_alive() and not newer.is_alive()
    assert not errors
    expected = [] if len(fetched) == 1 else ["nikke-2026-11"]
    assert [hold.id for hold in results["newer"]] == expected
    assert [hold.id for hold in holds.known()] == expected
    assert [hold.id for hold in holds.load()] == expected

    # An expired memo must still fetch and enforce the next signed serial.
    monkeypatch.setattr(holds, "_MEMO", (time.time() - holds.FRESH_SECONDS - 1, [], 1))
    monkeypatch.setattr(indexfetch, "open_bytes", lambda *_a, **_k: payloads[1])
    assert [hold.id for hold in holds.load()] == ["nikke-2026-11"]


@pytest.mark.parametrize(
    "previous", [None, ["earlier-hold"], _ABSENT], ids=["empty", "earlier", "absent"]
)
def test_confirm_hold_save_failure_rolls_back_in_memory_override(tmp_path, monkeypatch, previous):
    """A failed config save must not leave the accepted id in memory.

    Otherwise a retry of Play starts without asking, although nothing was saved.
    """
    widgets = pytest.importorskip("PyQt6.QtWidgets")
    qapp = widgets.QApplication.instance() or widgets.QApplication([])

    from nadare.gui.main_window import MainWindow
    from nadare.gui.notice import HoldDialog

    item = holds.parse(_doc(_hold()), url=URL)[0]
    monkeypatch.setattr(updatecheck, "check_latest", lambda: None)
    monkeypatch.setattr(notices, "load", lambda *a, **k: [])
    monkeypatch.setattr(holds, "load", lambda *a, **k: [item])
    monkeypatch.setattr(holds, "known", lambda *a, **k: [item])

    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="launcher-nikke", title="G")
    if previous is not None and previous is not _ABSENT:
        config.profile("g")["hold_override"] = previous
    config.save()

    window = MainWindow(Config.load(path))
    if previous is _ABSENT:
        window.config.profile("g").pop("hold_override", None)
    errors: list[str] = []
    monkeypatch.setattr(
        widgets.QMessageBox,
        "critical",
        lambda _parent, _title, message: (
            errors.append(message) or widgets.QMessageBox.StandardButton.Ok
        ),
    )
    monkeypatch.setattr(HoldDialog, "exec", lambda self: widgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(window.config, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))

    try:
        assert window._confirm_hold("g") is False
        assert errors, "expected a save-failure dialog"
        profile = window.config.profile("g")
        if previous is _ABSENT:
            assert "hold_override" not in profile
        elif previous is None:
            # A fresh profile has the empty default, not a missing key.
            assert profile["hold_override"] == ""
        else:
            assert profile["hold_override"] == previous

        # The in-memory profile must not keep the unsaved id: a retry asks again.
        asked: list[str] = []

        def reject(dialog):
            asked.append(dialog.hold.id)
            return widgets.QDialog.DialogCode.Rejected

        monkeypatch.setattr(HoldDialog, "exec", reject)
        assert window._confirm_hold("g") is False
        assert asked == [item.id]
        assert holds.blocking_hold(window.config, "g", "launcher-nikke", holds=[item]) == item
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_known_falls_through_bad_primary_mirror_cache_to_second_mirror(signing_env, monkeypatch):
    """A bad cached copy on mirror 1 must not hide mirror 2's valid signed copy."""
    key, fingerprint, _tmp_path = signing_env
    first = "https://m1.invalid/index/"
    second = "https://m2.invalid/index/"
    monkeypatch.setenv("NADARE_INDEX_BASE", f"{first} {second}")

    bad = indexfetch.cache_path(first, holds.CACHE_REL)
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"body": "tampered", "signature": "bad"}), encoding="utf-8")

    good = indexfetch.cache_path(second, holds.CACHE_REL)
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        json.dumps(_signed_envelope(_doc(_hold(), serial=1), key, fingerprint)),
        encoding="utf-8",
    )

    assert not holds.serial_path().exists()
    assert [hold.id for hold in holds.known()] == ["nikke-2026-11"]


def test_disabled_index_load_and_known_agree(signing_env, monkeypatch):
    """Turning fetching off keeps an already accepted hold enforced by both paths."""
    key, fingerprint, _tmp_path = signing_env
    payload = json.dumps(_signed_envelope(_doc(_hold(), serial=1), key, fingerprint)).encode(
        "utf-8"
    )
    assert [hold.id for hold in holds.load(opener=lambda _url: payload)] == ["nikke-2026-11"]

    monkeypatch.setattr(holds, "_MEMO", None)
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert [hold.id for hold in holds.load()] == ["nikke-2026-11"]
    assert holds.load() == holds.known()
