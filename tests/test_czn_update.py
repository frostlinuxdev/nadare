# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic tests for the CZN updater's verification and probing.

Chunks are served by a local HTTP server (directdl does the download, #1557).
"""

from __future__ import annotations

import gzip
import hashlib

import pytest
from test_directdl_prune import server  # noqa: F401 - fixture reuse

from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend_parts import launching
from nadare.core import directdl
from nadare.core.yostar import ManifestError


def _pack(payload: bytes) -> bytes:
    return gzip.compress(payload, mtime=0)


# ---------------------------------------------------------------------------
# F7 — verification must fail closed (the chunks now go through directdl, #1557)
# ---------------------------------------------------------------------------
def test_a_row_with_an_empty_manifest_md5_is_refused(monkeypatch, tmp_path) -> None:
    """An empty manifest md5 column must refuse the chunk, not skip the check."""
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path)
    with pytest.raises(ManifestError):
        czn_update.to_plan([czn_update.Row("/bin/a.dll", "3", "c0", "", "", None)])


def test_a_row_with_a_bad_unpacked_md5_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path)
    packed = _pack(b"payload")
    with pytest.raises(ManifestError):
        czn_update.to_plan(
            [czn_update.Row("/bin/a.dll", "3", "c0", "not-a-hash",
                            hashlib.md5(packed).hexdigest(), None)]
        )


def test_the_plan_checks_both_md5s_and_the_size(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path)
    monkeypatch.setattr(czn_update, "BASE", "https://cdn.example/game/dpms_X")
    payload = b"payload"
    packed = _pack(payload)
    (rf,) = czn_update.to_plan(
        [czn_update.Row("/bin/a.dll", "3", "c0", hashlib.md5(payload).hexdigest().upper(),
                        hashlib.md5(packed).hexdigest(), len(payload))]
    ).files
    assert rf.path == "bin/a.dll"
    assert rf.url == "https://cdn.example/game/dpms_X/v3/c0.gz"
    assert (rf.size, rf.md5) == (len(payload), hashlib.md5(payload).hexdigest())
    assert rf.packed == directdl.Packed("gzip", md5=hashlib.md5(packed).hexdigest())


def _sync_one(make_server, tmp_path, monkeypatch, payload, packed, usize):
    srv = make_server({"/v3/c0.gz": packed})
    monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "game")
    monkeypatch.setattr(czn_update, "BASE", srv.url)
    plan = czn_update.to_plan(
        [czn_update.Row("/a.bin", "3", "c0", hashlib.md5(payload).hexdigest(),
                        hashlib.md5(packed).hexdigest(), usize)]
    )
    directdl.sync(plan, tmp_path / "game")


def test_a_size_mismatch_is_refused(server, monkeypatch, tmp_path) -> None:  # noqa: F811
    """A chunk whose decompressed length differs from ``usize`` is refused."""
    payload = b"payload"
    with pytest.raises(directdl.DirectError):
        _sync_one(server, tmp_path, monkeypatch, payload, _pack(payload), len(payload) + 1)
    assert not (tmp_path / "game" / "a.bin").exists()


# ---------------------------------------------------------------------------
# F8 — a decompression bomb is bounded
# ---------------------------------------------------------------------------
def test_the_unpacked_size_is_capped_at_the_manifest_size(server, monkeypatch, tmp_path) -> None:  # noqa: F811
    """A chunk that expands past the file's size is aborted while it is decoded."""
    payload = b"x" * 1_000_000
    with pytest.raises(directdl.DirectError, match="larger than"):
        _sync_one(server, tmp_path, monkeypatch, payload, _pack(payload), 4096)
    assert not (tmp_path / "game" / "a.bin").exists()


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
# a lingering crash handler is not "the game" (#24); one rule with the backend (#1557)
# ---------------------------------------------------------------------------
class TestGameRunningIgnoresHelpers:
    PREFIX = "z:\\mnt\\games\\czn\\bin\\"

    @staticmethod
    def _argv(*tokens: str) -> bytes:
        return b"\0".join(t.encode() for t in tokens) + b"\0"

    @staticmethod
    def _game(raw: bytes, prefix: str) -> bool:
        return launching._cmdline_has_exe(raw, prefix, exclude=launching.GAME_HELPER_EXES)

    def test_the_game_exe_counts(self):
        raw = self._argv("Z:\\mnt\\games\\czn\\bin\\ChaosZeroNightmare.exe")
        assert self._game(raw, self.PREFIX) is True

    def test_a_lingering_crashpad_handler_does_not_count(self):
        raw = self._argv("Z:\\mnt\\games\\czn\\bin\\crashpad_handler.exe", "--database=x")
        assert self._game(raw, self.PREFIX) is False

    def test_helper_match_is_case_and_separator_insensitive(self):
        raw = self._argv("Z:/mnt/games/czn/bin/CrashPad_Handler.EXE")
        assert self._game(raw, self.PREFIX.replace("\\", "/")) is False

    def test_the_game_still_counts_next_to_its_crash_handler(self):
        raw = self._argv(
            "Z:\\mnt\\games\\czn\\bin\\crashpad_handler.exe",
            "Z:\\mnt\\games\\czn\\bin\\ssr-stove-shield.exe",
        )
        assert self._game(raw, self.PREFIX) is True

    def test_the_updater_has_no_helper_list_of_its_own(self):
        assert not hasattr(czn_update, "GAME_HELPER_EXES")
        assert czn_update.launching is launching


# ---------------------------------------------------------------------------
# optimisation audit: probe the version window concurrently
# ---------------------------------------------------------------------------
def _sequential_latest(published: set[int], installed: int) -> int:
    latest = installed if installed in published else 0
    v, misses = installed, 0
    while misses < 5:
        v += 1
        if v in published:
            latest, misses = v, 0
        else:
            misses += 1
    return latest


def test_concurrent_probe_agrees_with_the_sequential_walk(monkeypatch) -> None:
    import random

    rng = random.Random(7)
    for _ in range(60):
        installed = rng.randrange(0, 12)
        published = {
            n for n in range(installed - 2, installed + 40) if n > 0 and rng.random() < 0.6
        }
        monkeypatch.setattr(
            czn_update, "_head_ok", lambda url, p=published: int(url.rsplit("_", 1)[1][:-5]) in p
        )
        assert czn_update.latest_version(installed) == _sequential_latest(published, installed)


def test_the_probe_window_runs_concurrently(monkeypatch) -> None:
    import time

    def head(url: str) -> bool:
        time.sleep(0.1)
        return False

    monkeypatch.setattr(czn_update, "_head_ok", head)
    start = time.perf_counter()
    czn_update.latest_version(5)
    assert time.perf_counter() - start < 0.35  # 6 serial probes would take 0.6 s


def test_probe_budget_is_never_exceeded(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(czn_update, "_head_ok", lambda url: calls.append(url) or True)
    czn_update.latest_version(0, max_probes=12)
    assert len(calls) <= 12
