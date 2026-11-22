# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression coverage for saved runner pins and malformed release signatures."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_release_signature import sign
from test_runner_manifest import _Clock, _row

from nadare.core import indexfetch, releasesig, runnerindex, runners, selfupdate


@pytest.fixture
def signed_feed(monkeypatch):
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = b"\x42" * 20
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: verify(p, s, key=pinned))
    clock = _Clock()
    monkeypatch.setattr(runnerindex, "time", clock)
    feed = {"doc": {}, "error": None, "clock": clock}

    def fetch(url, **_kwargs):
        if feed["error"]:
            raise feed["error"]
        body = json.dumps(feed["doc"], ensure_ascii=False).encode("utf-8")
        assert len(body) <= 65536
        return sign(body, key, fingerprint).encode("ascii") if url.endswith(".asc") else body

    monkeypatch.setattr(indexfetch, "open_bytes", fetch)
    return feed


def _large_doc(padding):
    return {"epoch": 10, "runners": {"dwproton": _row("dwproton-11.0-20"), **padding}}


@pytest.mark.parametrize("padding", [
    {"future": {"note": "é" * 23000}},
    {"future": {"note": "😀" * 15000}},
    {"\\" * 16000: {"note": "\\" * 16000}},
])
def test_large_signed_state_keeps_pin_after_outage(signed_feed, padding):
    signed_feed["doc"] = _large_doc(padding)
    assert len(runnerindex.refresh()) == 1
    state = json.loads(runnerindex.cache_path().read_bytes())
    assert runnerindex._read() == state
    for character in ("é", "😀"):
        if character in state["accepted"]["body"]:
            assert character.encode() in runnerindex.cache_path().read_bytes()
    # Older versions wrote ASCII escapes; those accepted watermarks must recover too.
    runnerindex.cache_path().write_text(json.dumps(state), encoding="utf-8")
    assert runnerindex._read() == state
    assert runners.pinned_tag("dwproton") == "dwproton-11.0-20"
    # Exercise the failure write as well as the successful write.
    signed_feed["clock"].now += runnerindex.REFRESH_SECONDS + 1
    signed_feed["error"] = OSError("offline")
    assert runnerindex.refresh() == []
    assert runnerindex._read()["accepted"] == state["accepted"]
    for character in ("é", "😀"):
        if character in state["accepted"]["body"]:
            assert character.encode() in runnerindex.cache_path().read_bytes()
    assert runners.pinned_tag("dwproton") == "dwproton-11.0-20"


def test_large_signed_state_rejects_replay_after_failed_refresh(signed_feed):
    signed_feed["doc"] = _large_doc({"future": {"note": "é" * 23000}})
    assert runnerindex.refresh()
    signed_feed["clock"].now += runnerindex.REFRESH_SECONDS + 1
    signed_feed["error"] = OSError("offline")
    assert runnerindex.refresh() == []
    signed_feed["clock"].now += runnerindex.REFRESH_SECONDS + 1
    signed_feed["error"] = None
    # A newer tag at an older epoch proves the epoch guard, independently of rollback checks.
    signed_feed["doc"] = {"epoch": 1, "runners": {"dwproton": _row("dwproton-11.0-21")}}
    assert runnerindex.refresh() == []
    assert runners.pinned_tag("dwproton") == "dwproton-11.0-20"
    assert runnerindex._read()["error"] == "replayed epoch"


def test_archive_name_rejects_nul(signed_feed):
    with pytest.raises(ValueError):
        runnerindex.plain_file_name("dwproton-11.0-19\0.tar.xz")
    signed_feed["doc"] = {"epoch": 1, "runners": {
        "dwproton": _row("dwproton-11.0-19", file="dwproton-11.0-19\0.tar.xz"),
    }}
    assert runnerindex.refresh() == []
    assert runners.pinned_tag("dwproton") == runners.RUNNERS["dwproton"].tested[0]


@pytest.mark.parametrize("suffix", ["rc1", "rc2", "alpha1", "beta2", "pre1", "preview1"])
def test_final_release_follows_prerelease(signed_feed, suffix):
    tag = "dwproton-11.0-19-" + suffix
    signed_feed["doc"] = {"epoch": 1, "runners": {"dwproton": _row(tag)}}
    assert runnerindex.refresh()
    signed_feed["clock"].now += runnerindex.REFRESH_SECONDS + 1
    signed_feed["doc"] = {"epoch": 2, "runners": {"dwproton": _row("dwproton-11.0-19")}}
    change, = runnerindex.refresh()
    assert change.previous_tag == tag
    assert change.tag == "dwproton-11.0-19"
    assert runners.pinned_tag("dwproton") == change.tag
    # Going back to the prerelease still requires explicit rollback authorization.
    signed_feed["clock"].now += runnerindex.REFRESH_SECONDS + 1
    signed_feed["doc"] = {"epoch": 3, "runners": {"dwproton": _row(tag)}}
    assert runnerindex.refresh() == []
    assert runners.pinned_tag("dwproton") == change.tag


def test_prerelease_cannot_undercut_builtin_final(signed_feed):
    floor = runners.RUNNERS["dwproton"].tested[0]
    signed_feed["doc"] = {"epoch": 1, "runners": {"dwproton": _row(floor + "-rc1")}}
    assert runnerindex.refresh() == []
    assert runners.pinned_tag("dwproton") == floor
    assert runnerindex._read()["error"] == "runner is older than the built-in pin"


# Each length header is incomplete, including slices that used to silently read as zero.
@pytest.mark.parametrize("packet", [
    b"\xc2", b"\xc2\xc0", b"\xc2\xff", b"\xc2\xff\0\0\0",
    b"\x88", b"\x89\0", b"\x8a\0\0\0",
])
def test_truncated_signature_header_has_updater_error(tmp_path, monkeypatch, packet):
    payload = tmp_path / "Nadare.AppImage"
    payload.write_bytes(b"fixture")
    armor = ("-----BEGIN PGP SIGNATURE-----\n\n" + base64.b64encode(packet).decode("ascii")
             + "\n-----END PGP SIGNATURE-----\n")
    # Check the parser's contract too: later verification errors must not hide a bad header.
    with pytest.raises(releasesig.SignatureError, match="cut short"):
        list(releasesig._packets(packet))
    monkeypatch.setattr(selfupdate, "_fetch_sums", lambda _url: armor)
    urls = selfupdate.AssetUrls(
        "https://fixture.invalid/app", "https://fixture.invalid/sums", "app",
    )
    with pytest.raises(selfupdate.SelfUpdateError, match="not installed"):
        selfupdate.verify_signature(urls, payload)


@pytest.mark.parametrize("area", [b"\xc0", b"\xff", b"\xff\0\0\0"])
def test_truncated_signature_subpacket_length_is_rejected(area):
    with pytest.raises(releasesig.SignatureError, match="damaged"):
        releasesig._subpackets(area)
