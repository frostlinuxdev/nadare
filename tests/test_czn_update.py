# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the CZN updater's verification and probing.

``_get`` is replaced with an in-memory fake so no test touches the network.
"""

from __future__ import annotations

import gzip
import hashlib

import pytest

from nadare.backends.czn import update as czn_update
from nadare.core.yostar import ManifestError


def _pack(payload: bytes) -> bytes:
    return gzip.compress(payload)


# ---------------------------------------------------------------------------
# F7 — verification must fail closed
# ---------------------------------------------------------------------------
def test_fetch_verified_rejects_empty_manifest_md5(monkeypatch) -> None:
    """An empty manifest md5 column must refuse the chunk, not skip the check."""
    packed = _pack(b"payload")
    monkeypatch.setattr(czn_update, "_get", lambda *a, **k: packed)
    with pytest.raises(ManifestError):
        czn_update._fetch_verified("/bin/a.dll", "3", "c0", "", "")


def test_fetch_verified_rejects_a_bad_unpacked_md5(monkeypatch) -> None:
    payload = b"payload"
    packed = _pack(payload)
    monkeypatch.setattr(czn_update, "_get", lambda *a, **k: packed)
    with pytest.raises(ManifestError):
        czn_update._fetch_verified(
            "/bin/a.dll", "3", "c0", "not-a-hash", hashlib.md5(packed).hexdigest()
        )


def test_download_file_rejects_a_size_mismatch(monkeypatch, tmp_path) -> None:
    """A chunk whose decompressed length differs from ``usize`` is refused."""
    game_dir = tmp_path / "game"
    monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
    payload = b"payload"
    packed = _pack(payload)
    monkeypatch.setattr(czn_update, "_get", lambda *a, **k: packed)
    with pytest.raises(ValueError):
        czn_update.download_file(
            "/a.bin", "3", "c0", hashlib.md5(payload).hexdigest(),
            hashlib.md5(packed).hexdigest(), len(payload) + 1,
        )
    assert not (game_dir / "a.bin").exists()


# ---------------------------------------------------------------------------
# F8 — a decompression bomb is bounded
# ---------------------------------------------------------------------------
def test_fetch_verified_caps_the_unpacked_size(monkeypatch) -> None:
    """A chunk that expands past the cap is aborted instead of buffered whole."""
    monkeypatch.setattr(czn_update, "MAX_UNPACKED", 4096)
    payload = b"x" * 1_000_000
    packed = _pack(payload)
    monkeypatch.setattr(czn_update, "_get", lambda *a, **k: packed)
    with pytest.raises(ValueError):
        czn_update._fetch_verified(
            "/bomb.bin",
            "3",
            "c0",
            hashlib.md5(payload).hexdigest(),
            hashlib.md5(packed).hexdigest(),
        )


# ---------------------------------------------------------------------------
# backend F8 — the upward probe is bounded
# ---------------------------------------------------------------------------
def test_latest_version_bounds_the_probe(monkeypatch) -> None:
    """A monotonic run of hits must not send thousands of requests."""
    calls: list[str] = []

    def _num(url: str) -> int:
        try:
            return int(url.rsplit("_", 1)[1].removesuffix(".json"))
        except (IndexError, ValueError):
            return 0

    def head(url: str) -> bool:
        calls.append(url)
        # Many consecutive manifests exist, then the numbering stops.
        return _num(url) <= 1000

    monkeypatch.setattr(czn_update, "_head_ok", head)
    latest = czn_update.latest_version(0)
    assert latest > 0
    assert len(calls) <= czn_update.MAX_PROBES


def test_latest_version_stops_after_five_misses(monkeypatch) -> None:
    calls: list[str] = []

    def head(url: str) -> bool:
        calls.append(url)
        return False

    monkeypatch.setattr(czn_update, "_head_ok", head)
    assert czn_update.latest_version(1) == 0
    assert len(calls) <= 6  # the installed probe plus at most five misses


# ---------------------------------------------------------------------------
# Round-1 audit: F3 / F4 / F5
# ---------------------------------------------------------------------------
def test_plan_matches_uppercase_manifest_md5(monkeypatch, tmp_path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "a.bin").write_bytes(b"data")
    monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
    md5 = hashlib.md5(b"data").hexdigest().upper()
    monkeypatch.setattr(
        czn_update, "parse_files", lambda m: [("F", "/a.bin", "3", "c0", md5, "p", 4)]
    )
    assert czn_update.plan({})[0] == []


def test_latest_version_zero_finds_v1(monkeypatch) -> None:
    monkeypatch.setattr(czn_update, "_head_ok", lambda url: url.endswith("_1.json"))
    assert czn_update.latest_version(0) == 1


def test_plan_nul_path_is_a_clean_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path)
    monkeypatch.setattr(
        czn_update, "parse_files", lambda m: [("F", "/a\x00b", "3", "c0", "0" * 32, "p", 1)]
    )
    with pytest.raises(ManifestError):
        czn_update.plan({})


# ---------------------------------------------------------------------------
# a lingering crash handler is not "the game" (#24)
# ---------------------------------------------------------------------------
class TestGameRunningIgnoresHelpers:
    PREFIX = "z:\\mnt\\games\\czn\\bin\\"

    @staticmethod
    def _argv(*tokens: str) -> bytes:
        return b"\0".join(t.encode() for t in tokens) + b"\0"

    def test_the_game_exe_counts(self):
        raw = self._argv("Z:\\mnt\\games\\czn\\bin\\ChaosZeroNightmare.exe")
        assert czn_update._cmdline_has_exe(raw, self.PREFIX) is True

    def test_a_lingering_crashpad_handler_does_not_count(self):
        raw = self._argv("Z:\\mnt\\games\\czn\\bin\\crashpad_handler.exe", "--database=x")
        assert czn_update._cmdline_has_exe(raw, self.PREFIX) is False

    def test_helper_match_is_case_and_separator_insensitive(self):
        raw = self._argv("Z:/mnt/games/czn/bin/CrashPad_Handler.EXE")
        assert czn_update._cmdline_has_exe(raw, self.PREFIX.replace("\\", "/")) is False

    def test_the_game_still_counts_next_to_its_crash_handler(self):
        raw = self._argv(
            "Z:\\mnt\\games\\czn\\bin\\crashpad_handler.exe",
            "Z:\\mnt\\games\\czn\\bin\\ssr-stove-shield.exe",
        )
        assert czn_update._cmdline_has_exe(raw, self.PREFIX) is True

    def test_backend_and_updater_agree_on_the_helper_list(self):
        from nadare.backends.czn import backend

        assert czn_update.GAME_HELPER_EXES == backend.GAME_HELPER_EXES
