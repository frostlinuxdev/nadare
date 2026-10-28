# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the shared Visual C++ runtime helper.

Nothing here may open a socket: ``download.http_open`` is replaced in every
test (an autouse fixture makes it raise ``OSError`` by default, and tests that
exercise the download path substitute their own byte server).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nadare.core import indexfetch, vcredist
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

BUILTIN = b"MZ" + b"\x00" * 10 + b"Wine builtin DLL" + b"\x00" * 32

INDEX_BASE = "https://idx.example/"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _offline(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr(vcredist._net, "http_open", _offline)


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path, monkeypatch):
    """Every index test gets its own cache and a single test base URL."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("NADARE_INDEX_BASE", INDEX_BASE)


def _write_dlls(prefix: Path, *, native: bool = True) -> None:
    root = prefix / "drive_c/windows/system32"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("vcruntime140.dll", "msvcp140.dll"):
        (root / name).write_bytes(b"MZ-native" if native else BUILTIN)


class _BytesResp:
    """A minimal context-manager response for the index fetches."""

    def __init__(self, payload: bytes, *, final: str | None = None) -> None:
        self._payload = payload
        self._read = False
        self._final = final
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def geturl(self) -> str:
        return self._final or vcredist.VCREDIST_URL

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._payload


def _serve(payload: bytes, *, final: str | None = None):
    def _open(*_a, **_k):
        return _BytesResp(payload, final=final)

    return _open


def _serve_index(payload: bytes):
    def _open(*_a, **_k):
        return payload

    return _open


# ---------------------------------------------------------------------------
# installed()
# ---------------------------------------------------------------------------
class TestInstalled:
    def test_native_runtime(self, tmp_path):
        _write_dlls(tmp_path, native=True)
        assert vcredist.installed(tmp_path) is True

    def test_builtin_placeholder(self, tmp_path):
        _write_dlls(tmp_path, native=False)
        assert vcredist.installed(tmp_path) is False

    def test_missing_files(self, tmp_path):
        assert vcredist.installed(tmp_path) is False

    def test_marker_beyond_the_first_4096_bytes_is_ignored(self, tmp_path):
        root = tmp_path / "drive_c/windows/system32"
        root.mkdir(parents=True, exist_ok=True)
        late = b"MZ" + b"\x00" * 5000 + b"Wine builtin DLL"
        for name in ("vcruntime140.dll", "msvcp140.dll"):
            (root / name).write_bytes(late)
        assert vcredist.installed(tmp_path) is True


# ---------------------------------------------------------------------------
# pin resolution + index
# ---------------------------------------------------------------------------
class TestPins:
    def test_env_beats_profile_and_constant(self):
        digest = "a" * 64
        pin = vcredist.resolve_pin(
            {"vcredist_sha256": "b" * 64},
            constant="c" * 64,
            environ={"NADARE_VCREDIST_SHA256": digest},
        )
        assert pin == digest

    def test_profile_beats_constant(self):
        pin = vcredist.resolve_pin(
            {"vcredist_sha256": "b" * 64}, constant="c" * 64, environ={}
        )
        assert pin == "b" * 64

    def test_constant_is_the_fallback(self):
        assert vcredist.resolve_pin({}, constant="c" * 64, environ={}) == "c" * 64

    def test_whitespace_falls_through(self):
        assert (
            vcredist.resolve_pin(
                {"vcredist_sha256": "   "}, constant="c" * 64, environ={}
            )
            == "c" * 64
        )

    def test_overridden_only_when_explicit(self):
        assert vcredist.overridden({}, environ={}) is False
        assert vcredist.overridden({}, environ={"NADARE_VCREDIST_SHA256": "a" * 64}) is True

    def test_index_pins_parses_and_caps(self, monkeypatch):
        good = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(12)]
        payload = json.dumps({"sha256": [*good, "not-a-hash"]}).encode()
        monkeypatch.setattr(indexfetch, "open_bytes", _serve_index(payload))
        pins = vcredist.index_pins()
        assert pins == set(good[: vcredist.VCREDIST_INDEX_MAX_HASHES])

    def test_index_pins_empty_on_bad_payload(self, monkeypatch):
        monkeypatch.setattr(indexfetch, "open_bytes", _serve_index(b"{}"))
        assert vcredist.index_pins() == set()

    def test_index_source_accepts_a_microsoft_url(self, monkeypatch):
        url = "https://download.visualstudio.microsoft.com/download/x/vc_redist.x64.exe"
        payload = json.dumps({"url": url}).encode()
        monkeypatch.setattr(indexfetch, "open_bytes", _serve_index(payload))
        assert vcredist.index_source() == url

    def test_index_source_rejects_an_off_microsoft_url(self, monkeypatch):
        payload = json.dumps({"url": "https://evil.example/vc_redist.x64.exe"}).encode()
        monkeypatch.setattr(indexfetch, "open_bytes", _serve_index(payload))
        assert vcredist.index_source() == ""


# ---------------------------------------------------------------------------
# verify() / download()
# ---------------------------------------------------------------------------
class TestVerify:
    def test_mismatch_is_deleted_and_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vcredist, "index_pins", lambda: set())
        inst = tmp_path / "inst.exe"
        inst.write_bytes(b"MZ-tampered")
        with pytest.raises(BackendError):
            vcredist.verify(
                inst, profile={}, emit=lambda _e: None, cancel=Cancel(), constant="0" * 64
            )
        assert not inst.exists()

    def test_index_pin_is_accepted(self, tmp_path):
        payload = b"MZ-indexed"
        digest = hashlib.sha256(payload).hexdigest()
        inst = tmp_path / "inst.exe"
        inst.write_bytes(payload)
        vcredist.verify(
            inst,
            profile={},
            emit=lambda _e: None,
            cancel=Cancel(),
            constant="0" * 64,
            index_pins_fn=lambda: {digest},
        )
        assert inst.exists()

    def test_missing_pin_is_refused(self, tmp_path):
        inst = tmp_path / "inst.exe"
        inst.write_bytes(b"MZ")
        with pytest.raises(BackendError):
            vcredist.verify(inst, profile={}, emit=lambda _e: None, cancel=Cancel(), constant=None)


class TestDownload:
    def test_off_microsoft_request_is_refused_before_fetching(self, tmp_path):
        with pytest.raises(BackendError):
            vcredist.download(
                "https://evil.example/vc.exe",
                tmp_path / "t",
                tmp_path / "i",
                verify_fn=lambda _p: None,
                emit=lambda _e: None,
                cancel=Cancel(),
            )

    def test_off_microsoft_redirect_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            vcredist._net,
            "http_open",
            _serve(b"MZ", final="https://evil.example/vc.exe"),
        )
        with pytest.raises(BackendError):
            vcredist.download(
                vcredist.VCREDIST_URL,
                tmp_path / "t",
                tmp_path / "i",
                verify_fn=lambda _p: None,
                emit=lambda _e: None,
                cancel=Cancel(),
            )


# ---------------------------------------------------------------------------
# ensure()
# ---------------------------------------------------------------------------
class TestEnsure:
    def _ensure(self, tmp_path, monkeypatch, payload, code, *, constant=None, cancel=None):
        monkeypatch.setattr(vcredist._net, "http_open", _serve(payload))
        ran: list[list[str]] = []
        events: list = []
        prefix = tmp_path / "prefix"
        vcredist.ensure(
            prefix,
            profile={},
            cache_dir=tmp_path / "cache",
            run_in_prefix=lambda argv: ran.append(list(argv)) or code,
            emit=events.append,
            cancel=cancel or Cancel(),
            constant=constant or hashlib.sha256(payload).hexdigest(),
        )
        return ran, events

    def test_skips_when_already_installed(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        _write_dlls(prefix, native=True)
        called: list = []
        monkeypatch.setattr(vcredist._net, "http_open", lambda *a, **k: called.append(1))
        vcredist.ensure(
            prefix,
            profile={},
            cache_dir=tmp_path / "cache",
            run_in_prefix=lambda argv: called.append(list(argv)) or 0,
            emit=lambda _e: None,
            cancel=Cancel(),
        )
        assert called == []

    def test_success_invokes_the_installer_with_quiet_flags(self, tmp_path, monkeypatch):
        payload = b"MZ-payload"
        ran, events = self._ensure(tmp_path, monkeypatch, payload, 0)
        assert ran and ran[0][1:] == ["/install", "/quiet", "/norestart"]
        assert (tmp_path / "cache" / "vc_redist.x64.exe").is_file()
        assert not any(e.kind == "status" for e in events)
        # Never log file contents.
        assert not any("MZ-payload" in e.message for e in events)

    @pytest.mark.parametrize("code", [0, 1638, 3010])
    def test_accepted_exit_codes(self, tmp_path, monkeypatch, code):
        ran, events = self._ensure(tmp_path, monkeypatch, b"MZ-ok", code)
        assert ran
        assert not any(e.kind == "status" and "not installed" in e.message for e in events)

    def test_hash_mismatch_warns_and_never_runs(self, tmp_path, monkeypatch):
        ran, events = self._ensure(
            tmp_path, monkeypatch, b"MZ-tampered", 0, constant="0" * 64
        )
        assert ran == []
        assert not (tmp_path / "cache" / "vc_redist.x64.exe").exists()
        assert any(
            e.kind == "status" and "Could not verify the Microsoft Visual C++ download" in e.message
            for e in events
        )

    def test_unexpected_exit_code_is_just_a_warning(self, tmp_path, monkeypatch):
        ran, events = self._ensure(tmp_path, monkeypatch, b"MZ-code5", 5)
        assert ran
        assert any(e.kind == "status" and "not installed" in e.message for e in events)

    def test_offline_download_is_just_a_warning(self, tmp_path, monkeypatch):
        events: list = []
        vcredist.ensure(
            tmp_path / "prefix",
            profile={},
            cache_dir=tmp_path / "cache",
            run_in_prefix=lambda _argv: 0,
            emit=events.append,
            cancel=Cancel(),
        )
        assert any(e.kind == "status" and "not installed" in e.message for e in events)

    def test_exactly_one_status_event_on_failure(self, tmp_path, monkeypatch):
        _ran, events = self._ensure(
            tmp_path, monkeypatch, b"MZ-tampered", 0, constant="0" * 64
        )
        assert sum(1 for e in events if e.kind == "status") == 1

    def test_cancellation_propagates(self, tmp_path):
        def _cancel() -> None:
            raise Cancelled()

        with pytest.raises(Cancelled):
            vcredist.ensure(
                tmp_path / "prefix",
                profile={},
                cache_dir=tmp_path / "cache",
                run_in_prefix=lambda _argv: 0,
                emit=lambda _e: None,
                cancel=Cancel(),
                install_fn=_cancel,
            )


def test_index_source_host_suffix_is_label_bounded(monkeypatch):
    ok = ["https://aka.ms/vs/17/release/vc_redist.x64.exe", "https://www.aka.ms/x.exe"]
    for url in ok:
        monkeypatch.setattr(
            indexfetch, "open_bytes", _serve_index(json.dumps({"url": url}).encode())
        )
        assert vcredist.index_source() == url
    monkeypatch.setattr(
        indexfetch,
        "open_bytes",
        _serve_index(json.dumps({"url": "https://evilaka.ms/x.exe"}).encode()),
    )
    assert vcredist.index_source() == ""


def test_index_pins_junk_up_front_does_not_hide_real_hashes(monkeypatch):
    """#97: the cap applies to valid hashes, not to the raw list."""
    good = hashlib.sha256(b"real").hexdigest()
    payload = json.dumps({"sha256": ["junk"] * 20 + [good]}).encode()
    monkeypatch.setattr(indexfetch, "open_bytes", _serve_index(payload))
    assert vcredist.index_pins() == {good}
