# SPDX-License-Identifier: GPL-3.0-or-later
"""#806: the read-only Safety check engine.

The indicator list is data from a signed index document. Everything here uses obvious stand-ins
("bad.example", "example-agent.dll"): no real incident indicator belongs in the tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_release_signature import sign

from nadare.core import indexfetch, paths, releasesig, safetycheck


def _doc(*indicators: dict, serial: int = 1) -> dict:
    return {"version": 1, "serial": serial, "indicators": list(indicators)}


def _indicator(**overrides) -> dict:
    row = {
        "id": "fake-indicator",
        "title": "Example marker",
        "kinds": ["*"],
        "level": "exposed",
        "rule": {"type": "path_exists", "glob": "bad.example"},
    }
    row.update(overrides)
    return row


def _parse(*indicators: dict):
    parsed, _skipped = safetycheck.parse_indicators(_doc(*indicators))
    return parsed


def _profile(tmp_path: Path, kind: str = "direct-pgr") -> tuple[Path, dict]:
    prefix = tmp_path / "prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    return prefix, {"backend": kind, "prefix": str(prefix)}


def _check(profile: dict, *indicators: dict, **kwargs):
    return safetycheck.check_profile("sample", profile, _parse(*indicators), **kwargs)


# ---- the rule types -----------------------------------------------------------------------
def test_a_clean_prefix_is_clean(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "game.exe").write_text("nothing to see")
    result = _check(profile, _indicator())
    assert result.level == "clean"
    assert result.matches == ()
    assert result.files_visited >= 1


def test_path_exists_matches_a_planted_file(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "example-agent.dll").write_text("")
    result = _check(
        profile,
        _indicator(rule={"type": "path_exists", "glob": "example-agent.dll"}),
    )
    assert result.level == "exposed"
    assert [(match.id, match.count) for match in result.matches] == [("fake-indicator", 1)]


def test_path_exists_accepts_a_nested_glob(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "ExampleApp" / "example_service").mkdir(parents=True)
    (prefix / "ExampleApp" / "example_service" / "example.log").write_text("")
    result = _check(
        profile,
        _indicator(
            rule={"type": "path_exists", "glob": "ExampleApp/example_service/example.log"}
        ),
    )
    assert result.level == "exposed"


def test_file_contains_matches_a_literal_text(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "example.log").write_text("first line\nexample-agent started\nlast line\n")
    result = _check(
        profile,
        _indicator(
            rule={"type": "file_contains", "glob": "example.log", "text": "agent started"}
        ),
    )
    assert result.level == "exposed"
    assert result.matches[0].count == 1


def test_file_contains_counts_every_matching_file(tmp_path):
    prefix, profile = _profile(tmp_path)
    for name in ("a.log", "b.log"):
        (prefix / name).write_text("marker")
    result = _check(
        profile,
        _indicator(rule={"type": "file_contains", "glob": "*.log", "text": "marker"}),
    )
    assert result.matches[0].count == 2


def test_file_size_matches_a_range(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "payload.bin").write_bytes(b"x" * 123)
    hit = _check(
        profile,
        _indicator(rule={"type": "file_size", "glob": "payload.bin", "min": 100, "max": 200}),
    )
    miss = _check(
        profile,
        _indicator(rule={"type": "file_size", "glob": "payload.bin", "min": 200, "max": 300}),
    )
    assert hit.level == "exposed" and hit.matches[0].count == 1
    assert miss.level == "clean"


def test_a_rule_only_looks_inside_the_profiles_folders(tmp_path):
    prefix, profile = _profile(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "bad.example").write_text("")
    install = tmp_path / "install"
    install.mkdir()
    (install / "bad.example").write_text("")
    profile["install_dir"] = str(install)
    result = _check(profile, _indicator())
    # One in the install folder, none from the sibling folder beside it.
    assert result.matches[0].count == 1


def test_an_install_folder_outside_the_prefix_is_checked_too(tmp_path):
    prefix, profile = _profile(tmp_path)
    install = tmp_path / "install"
    install.mkdir()
    (install / "bad.example").write_text("")
    profile["install_dir"] = str(install)
    result = _check(profile, _indicator())
    assert result.matches[0].count == 1


# ---- levels --------------------------------------------------------------------------------
def test_compromised_wins_over_exposed(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "loader.log").write_text("loader")
    (prefix / "agent.dll").write_text("")
    result = _check(
        profile,
        _indicator(
            id="loader",
            level="exposed",
            rule={"type": "file_contains", "glob": "loader.log", "text": "loader"},
        ),
        _indicator(
            id="agent",
            level="compromised",
            rule={"type": "path_exists", "glob": "agent.dll"},
        ),
    )
    assert result.level == "compromised"
    assert {match.id for match in result.matches} == {"loader", "agent"}


def test_exposed_alone_stays_exposed(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "bad.example").write_text("")
    result = _check(profile, _indicator(level="exposed"))
    assert result.level == "exposed"


# ---- bounded and read-only -----------------------------------------------------------------
def test_an_unreadable_file_is_skipped_not_fatal(tmp_path):
    if os.geteuid() == 0:  # root can read a 000 file, so there is nothing to test
        pytest.skip("running as root")
    prefix, profile = _profile(tmp_path)
    target = prefix / "example.log"
    target.write_text("marker")
    target.chmod(0)
    try:
        result = _check(
            profile,
            _indicator(rule={"type": "file_contains", "glob": "example.log", "text": "marker"}),
        )
    finally:
        target.chmod(0o600)
    assert result.level == "incomplete"
    assert result.files_visited >= 1


def test_a_file_that_disappears_while_reading_is_skipped(tmp_path, monkeypatch):
    prefix, profile = _profile(tmp_path)
    (prefix / "example.log").write_text("marker")
    real_open = os.open

    def gone(path, flags, *args, **kwargs):
        if str(path).endswith("example.log"):
            raise FileNotFoundError(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safetycheck.os, "open", gone)
    result = _check(
        profile,
        _indicator(rule={"type": "file_contains", "glob": "example.log", "text": "marker"}),
    )
    assert result.level == "incomplete"


def test_a_file_swapped_for_a_fifo_does_not_block_the_check(tmp_path, monkeypatch):
    prefix, profile = _profile(tmp_path)
    (prefix / "example.log").write_text("marker")
    real_entries = safetycheck._entries

    def swapped(root, cancel, limit):
        found, cut = real_entries(root, cancel, limit)
        for rel, mode, _size, _path in found:
            if rel.endswith("example.log") and stat.S_ISREG(mode):
                target = prefix / rel
                target.unlink()
                os.mkfifo(target)
        return found, cut

    monkeypatch.setattr(safetycheck, "_entries", swapped)
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = _check(
                profile,
                _indicator(
                    rule={"type": "file_contains", "glob": "example.log", "text": "marker"}
                ),
            )
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the safety check blocked on a FIFO"
    assert "error" not in outcome
    assert outcome["result"].level == "incomplete"


def test_a_huge_file_is_read_only_up_to_the_cap(tmp_path):
    prefix, profile = _profile(tmp_path)
    cap = 4 * 1024 * 1024
    target = prefix / "example.log"
    with target.open("wb") as handle:
        handle.write(b"x" * (cap + 1024))
        handle.write(b"NEEDLE")
    result = _check(
        profile,
        _indicator(rule={"type": "file_contains", "glob": "example.log", "text": "NEEDLE"}),
    )
    assert result.level == "clean"
    assert result.bytes_read <= cap


def test_the_rule_own_read_cap_is_honoured(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "example.log").write_text("x" * 80 + "NEEDLE")
    result = _check(
        profile,
        _indicator(
            rule={
                "type": "file_contains",
                "glob": "example.log",
                "text": "NEEDLE",
                "max_bytes": 20,
            }
        ),
    )
    assert result.level == "clean"
    assert result.bytes_read <= 20


def test_a_symlink_is_never_followed_out_of_the_folder(tmp_path):
    prefix, profile = _profile(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bad.example").write_text("")
    (outside / "notes.txt").write_text("marker")
    (prefix / "link").symlink_to(outside, target_is_directory=True)
    (prefix / "notes.log").symlink_to(outside / "notes.txt")
    for indicator in (
        _indicator(rule={"type": "path_exists", "glob": "link/bad.example"}),
        _indicator(rule={"type": "file_contains", "glob": "notes.log", "text": "marker"}),
        _indicator(rule={"type": "file_size", "glob": "notes.log", "min": 1, "max": 1000}),
    ):
        result = _check(profile, indicator)
        assert result.level == "clean"


def test_at_most_twenty_thousand_entries_are_visited(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "bad.example").write_text("")
    result = _check(profile, _indicator(), max_files=0)
    assert result.truncated
    assert result.level == "incomplete"


def test_the_total_read_cap_stops_reading(tmp_path):
    prefix, profile = _profile(tmp_path)
    for name in ("a.log", "b.log"):
        (prefix / name).write_text("marker")
    result = _check(
        profile,
        _indicator(rule={"type": "file_contains", "glob": "*.log", "text": "marker"}),
        max_total_bytes=1,
    )
    assert result.bytes_read <= 1


def test_a_cancelled_check_stops_and_says_so(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "game.exe").write_text("")
    cancel = threading.Event()
    cancel.set()
    result = _check(profile, _indicator(), cancel=cancel)
    assert result.cancelled
    assert result.level == "cancelled"


def test_the_check_never_changes_a_file(tmp_path):
    prefix, profile = _profile(tmp_path)
    target = prefix / "example.log"
    target.write_text("marker")
    before = (target.stat().st_mtime_ns, target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    _check(
        profile,
        _indicator(rule={"type": "file_contains", "glob": "example.log", "text": "marker"}),
    )
    after = (target.stat().st_mtime_ns, target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    assert after == before


# ---- schema --------------------------------------------------------------------------------
@pytest.mark.parametrize("glob", ["../secret", "/etc/passwd", "", "a/../../b", "~/x"])
def test_an_escaping_or_absolute_glob_is_rejected(glob):
    parsed, skipped = safetycheck.parse_indicators(
        _doc(_indicator(rule={"type": "path_exists", "glob": glob}))
    )
    assert parsed == []
    assert skipped == 1


def test_a_malformed_indicator_is_skipped_but_the_rest_are_kept():
    parsed, skipped = safetycheck.parse_indicators(
        _doc(
            {"title": "no id", "level": "exposed", "rule": {"type": "path_exists", "glob": "x"}},
            _indicator(id="bad-level", level="maybe"),
            _indicator(id="bad-rule", rule={"type": "nonsense"}),
            _indicator(id="bad-size", rule={"type": "file_size", "glob": "x", "min": "a"}),
            _indicator(id="good"),
        )
    )
    assert [indicator.id for indicator in parsed] == ["good"]
    assert skipped == 4


def test_no_more_than_two_hundred_indicators_are_used():
    rows = [_indicator(id=f"i-{index}") for index in range(205)]
    parsed, _skipped = safetycheck.parse_indicators(_doc(*rows))
    assert len(parsed) == 200


def test_kinds_filter_which_profiles_an_indicator_applies_to(tmp_path):
    prefix, profile = _profile(tmp_path, kind="direct-pgr")
    (prefix / "bad.example").write_text("")
    indicator = _indicator(kinds=["launcher-nikke"])
    assert _check(profile, indicator).level == "clean"
    other_prefix, other = _profile(tmp_path / "other", kind="launcher-nikke")
    (other_prefix / "bad.example").write_text("")
    assert _check(other, indicator).level == "exposed"
    assert _check(profile, _indicator(kinds=["*"])).level == "exposed"


# ---- the signed indicator list -------------------------------------------------------------
@pytest.fixture
def signed_feed(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: verify(p, s, key=pinned))
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    state = {"doc": _doc(_indicator()), "signature": "good", "calls": [], "error": None}

    def fetch(url, **_kwargs):
        state["calls"].append(url)
        if state["error"]:
            raise state["error"]
        body = json.dumps(state["doc"]).encode()
        if url.endswith(".asc"):
            if state["signature"] == "missing":
                raise OSError("404")
            if state["signature"] == "wrong":
                body += b"tampered"
            return sign(body, key, fingerprint).encode()
        return body

    monkeypatch.setattr(indexfetch, "open_bytes", fetch)
    state["cache"] = tmp_path / "iocs-cache.json"
    state["serial"] = paths.state_dir() / "safety" / "iocs-serial.json"
    state["key"] = key
    state["fingerprint"] = fingerprint
    return state


def _load_indicators(feed) -> list | None:
    return safetycheck.load_indicators(cache=feed["cache"])


def test_a_verified_list_is_used(signed_feed):
    indicators = _load_indicators(signed_feed)
    assert [indicator.id for indicator in indicators] == ["fake-indicator"]
    assert any(url.endswith("iocs.json") for url in signed_feed["calls"])
    assert any(url.endswith("iocs.json.asc") for url in signed_feed["calls"])


@pytest.mark.parametrize("signature", ["wrong", "missing"])
def test_an_unverified_list_is_ignored(signed_feed, signature):
    signed_feed["signature"] = signature
    assert _load_indicators(signed_feed) is None
    assert not signed_feed["cache"].exists()


def test_the_last_verified_copy_is_used_when_no_mirror_answers(signed_feed):
    assert _load_indicators(signed_feed) is not None
    signed_feed["error"] = OSError("offline")
    indicators = _load_indicators(signed_feed)
    assert [indicator.id for indicator in indicators] == ["fake-indicator"]


def test_a_tampered_cache_is_not_trusted(signed_feed):
    assert _load_indicators(signed_feed) is not None
    signed_feed["cache"].write_text(
        json.dumps({"body": json.dumps(signed_feed["doc"]), "signature": "nope"})
    )
    signed_feed["error"] = OSError("offline")
    assert _load_indicators(signed_feed) is None


# ---- anti-rollback ---------------------------------------------------------------------------
def test_a_replayed_older_list_does_not_replace_a_newer_one(signed_feed):
    signed_feed["doc"] = _doc(_indicator(id="newer"), serial=2)
    newer_body = json.dumps(signed_feed["doc"]).encode()
    assert [i.id for i in _load_indicators(signed_feed)] == ["newer"]

    signed_feed["doc"] = _doc(_indicator(id="older"), serial=1)
    indicators = _load_indicators(signed_feed)

    assert [i.id for i in indicators] == ["newer"]
    watermark = json.loads(signed_feed["serial"].read_text())
    assert watermark["serial"] == 2
    assert watermark["sha256"] == hashlib.sha256(newer_body).hexdigest()


def test_a_changed_body_at_the_same_serial_is_rejected(signed_feed):
    signed_feed["doc"] = _doc(_indicator(id="first"), serial=3)
    assert [i.id for i in _load_indicators(signed_feed)] == ["first"]

    signed_feed["doc"] = _doc(_indicator(id="second"), serial=3)
    indicators = _load_indicators(signed_feed)

    assert [i.id for i in indicators] == ["first"]
    assert json.loads(signed_feed["serial"].read_text())["serial"] == 3


def test_a_list_without_a_serial_is_rejected(signed_feed):
    signed_feed["doc"] = {"version": 1, "indicators": [_indicator()]}
    assert _load_indicators(signed_feed) is None
    assert not signed_feed["cache"].exists()
    assert not signed_feed["serial"].exists()


@pytest.mark.parametrize("serial", [0, -1, "1", 1.0, True, None])
def test_a_serial_that_is_not_a_positive_integer_is_not_version_1(serial):
    with pytest.raises(ValueError):
        safetycheck.parse_indicators({"version": 1, "serial": serial, "indicators": []})


def test_a_replayed_saved_copy_is_not_used(signed_feed):
    signed_feed["doc"] = _doc(_indicator(id="newer"), serial=2)
    assert _load_indicators(signed_feed) is not None

    signed_feed["doc"] = _doc(_indicator(id="older"), serial=1)
    body = json.dumps(signed_feed["doc"]).encode()
    signature = sign(body, signed_feed["key"], signed_feed["fingerprint"])
    signed_feed["cache"].write_text(
        json.dumps({"body": body.decode(), "signature": signature})
    )
    signed_feed["error"] = OSError("offline")

    assert _load_indicators(signed_feed) is None


def test_a_corrupt_watermark_is_treated_as_missing(signed_feed):
    signed_feed["serial"].parent.mkdir(parents=True, exist_ok=True)
    signed_feed["serial"].write_text("{not json")
    indicators = _load_indicators(signed_feed)
    assert [i.id for i in indicators] == ["fake-indicator"]
    assert json.loads(signed_feed["serial"].read_text())["serial"] == 1


# ---- no list / result text -----------------------------------------------------------------
def test_without_a_list_the_check_says_why_it_cannot_run(tmp_path):
    _prefix, profile = _profile(tmp_path)
    result = safetycheck.check_profile("sample", profile, None)
    assert result.level == "no_list"
    text = safetycheck.result_summary(result)
    assert "known bad files" in text
    assert "not a problem with the game" in text
    assert "nothing" in text.lower()
    assert "No indicator list yet" not in text
    assert result.matches == ()


def test_a_verified_but_empty_list_is_clean_not_missing(tmp_path):
    _prefix, profile = _profile(tmp_path)
    result = safetycheck.check_profile("sample", profile, [])
    assert result.level == "clean"


def test_the_clean_text_says_it_is_weaker_than_it_sounds(tmp_path):
    _prefix, profile = _profile(tmp_path)
    result = _check(profile, _indicator())
    text = safetycheck.result_summary(result)
    assert text.startswith("Clean")
    assert "reinstall" in text.lower()


def test_the_summary_lists_ids_and_counts_but_never_file_contents(tmp_path):
    prefix, profile = _profile(tmp_path)
    (prefix / "example.log").write_text("SECRET-LOGIN-TICKET marker")
    result = _check(
        profile,
        _indicator(
            id="fake-marker",
            title="Example marker",
            rule={"type": "file_contains", "glob": "example.log", "text": "marker"},
        ),
    )
    text = safetycheck.result_summary(result)
    assert "fake-marker" in text and "1" in text
    assert "SECRET-LOGIN-TICKET" not in text


# ---- per-profile storage -------------------------------------------------------------------
def test_a_result_is_stored_per_profile_with_a_timestamp(tmp_path):
    store = tmp_path / "results.json"
    first = safetycheck.SafetyResult(
        level="exposed", matches=(), checked_at="2026-11-19T10:00:00+00:00"
    )
    second = safetycheck.SafetyResult(
        level="compromised", matches=(), checked_at="2026-11-19T11:00:00+00:00"
    )
    safetycheck.save_result("alpha", first, path=store)
    safetycheck.save_result("beta", second, path=store)
    assert safetycheck.load_result("alpha", path=store).level == "exposed"
    assert safetycheck.load_result("beta", path=store).level == "compromised"
    assert safetycheck.load_result("gamma", path=store) is None
    assert set(safetycheck.load_all_results(path=store)) == {"alpha", "beta"}
    assert "2026-11-19T10:00:00" in safetycheck.load_result("alpha", path=store).checked_at


def test_a_compromised_result_stays_until_dismissed(tmp_path):
    store = tmp_path / "results.json"
    result = safetycheck.SafetyResult(
        level="compromised", matches=(), checked_at="2026-11-19T10:00:00+00:00"
    )
    safetycheck.save_result("alpha", result, path=store)
    assert safetycheck.load_result("alpha", path=store).dismissed is False
    safetycheck.dismiss("alpha", path=store)
    assert safetycheck.load_result("alpha", path=store).dismissed is True
    # A new result is not dismissed by the old answer.
    safetycheck.save_result("alpha", result, path=store)
    assert safetycheck.load_result("alpha", path=store).dismissed is False


def test_a_broken_results_file_reads_as_no_results(tmp_path):
    store = tmp_path / "results.json"
    store.write_text("{not json")
    assert safetycheck.load_all_results(path=store) == {}
    assert safetycheck.load_result("alpha", path=store) is None
