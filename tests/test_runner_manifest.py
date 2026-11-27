# SPDX-License-Identifier: GPL-3.0-or-later
"""#1369: the recommended runner may come from a signed ``runners.json``.

The built-in pin is the floor: with no index, no network, an old app or any unusable document the
app behaves exactly as before. A document is used only when the release key signs it, it is not
older than the built-in pin, and its epoch does not go back. Signatures here are made with a
throwaway key in the same format gpg writes, so no real key or gpg is needed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from runner_layout import make_runner as make_runner_layout
from test_release_signature import sign

from nadare.core import download, hosting, indexfetch, releasesig, runners

OLD = "dwproton-11.0-17"
NEW = "dwproton-11.0-19"


class _Clock:
    """A stand-in for the ``time`` module inside :mod:`nadare.core.runnerindex` only."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


def _row(tag: str, **extra) -> dict:
    row = {
        "tag": tag,
        "sha256": hashlib.sha256(b"archive").hexdigest(),
        "size": 7,
        "published_at": "2026-11-19T00:00:00Z",
        "tested": {"yes": True, "date": "2026-11-19", "report": "https://example.org/test"},
    }
    row.update(extra)
    return row


@pytest.fixture
def feed(monkeypatch):
    from nadare.core import runnerindex

    monkeypatch.setenv("NADARE_INDEX_BASE", "https://fixture.invalid/index/")
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    fingerprint = hashlib.sha1(public).digest()
    pinned = releasesig.PinnedKey(fingerprint.hex().upper(), public)
    verify = releasesig.verify_detached
    monkeypatch.setattr(releasesig, "verify_detached", lambda p, s: verify(p, s, key=pinned))
    doc = {"epoch": 1, "runners": {"dwproton": _row(NEW, critical=True)}}
    clock = _Clock()
    monkeypatch.setattr(runnerindex, "time", clock)
    state = {
        "doc": doc,
        "signature": "good",
        "calls": [],
        "error": None,
        "refresh": runnerindex.refresh,
        "tick": lambda: setattr(clock, "now", clock.now + runnerindex.REFRESH_SECONDS + 1),
    }

    def fetch(url, **_kw):
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
    return state


def make_runner(tag: str):
    folder = runners.paths.runners_dir() / f"{tag}-x86_64"
    return make_runner_layout(folder)


@pytest.mark.parametrize("signature", ["wrong", "missing"])
def test_bad_signature_is_ignored_once(feed, signature, caplog):
    feed["signature"] = signature
    for _ in range(3):
        assert feed["refresh"]() == []
        assert runners.pinned_tag("dwproton") == OLD
    assert len(feed["calls"]) == 2
    assert sum("Ignoring runners.json" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("failure", [OSError("offline"), download.DownloadError("HTTP 503")])
def test_network_failure_backs_off_across_restarts(feed, failure):
    feed["error"] = failure
    assert feed["refresh"]() == []
    # There is no process-local state to preserve: throttle must live on disk.
    assert feed["refresh"]() == []
    assert len(feed["calls"]) == 1
    assert runners.pinned_tag("dwproton") == OLD
    feed["tick"]()
    feed["error"] = None
    assert feed["refresh"]()
    assert runners.pinned_tag("dwproton") == NEW


def test_an_outage_keeps_the_accepted_pin(feed):
    """A mirror that cannot be reached must not send a user back to an older build."""
    assert feed["refresh"]()
    feed["tick"]()
    feed["error"] = OSError("offline")
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW
    # and no retry storm: the failed attempt is still throttled
    tick_was = feed["tick"]
    assert feed["refresh"]() == []
    assert len(feed["calls"]) == 3
    tick_was()


@pytest.mark.parametrize(
    "tag,reason",
    [
        ("dwproton-11.0-16", "older than the built-in pin"),
        ("dwproton-11.0-17", "changes the built-in pin"),
    ],
)
def test_floor_cannot_be_replaced_even_at_a_high_epoch(feed, tag, reason, caplog):
    caplog.set_level(logging.WARNING)
    feed["doc"]["epoch"] = 999
    feed["doc"]["runners"]["dwproton"].update(tag=tag, sha256="f" * 64, previous_tag=NEW)
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD
    assert runners._spec("dwproton").tested == runners.RUNNERS["dwproton"].tested
    assert any(reason in record.message for record in caplog.records), caplog.text


@pytest.mark.parametrize("mode", ["epoch", "tag", "same_epoch", "wrong_previous"])
def test_replay_or_implicit_rollback_is_refused(feed, mode):
    feed["doc"]["epoch"] = 5
    assert feed["refresh"]()
    feed["tick"]()
    row = feed["doc"]["runners"]["dwproton"]
    feed["doc"]["epoch"] = 4 if mode == "epoch" else 5 if mode == "same_epoch" else 6
    row["tag"] = "dwproton-11.0-18"
    if mode == "wrong_previous":
        row["previous_tag"] = "dwproton-11.0-20"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW, "a refused file leaves the accepted pin alone"
    # A failed refresh must not erase the replay watermark: an older file stays refused.
    feed["tick"]()
    feed["doc"]["epoch"] = 4
    row["tag"] = "dwproton-11.0-20"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW


def test_explicit_signed_rollback_above_floor(feed):
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["epoch"] = 2
    feed["doc"]["runners"]["dwproton"].update(tag="dwproton-11.0-18", previous_tag=NEW)
    assert feed["refresh"]()
    assert runners.pinned_tag("dwproton") == "dwproton-11.0-18"


@pytest.mark.parametrize(
    "name",
    [
        NEW + "-x86_64.tar.xz",  # the derived name, written out
        "dwproton-11.0-19-custom-x86_64.tar.zst",
        "dwproton-11.0-19-custom-x86_64.tar.gz",
        NEW + "-" + "a" * (200 - len(NEW) - 1 - len(".tar.xz")) + ".tar.xz",  # the limit itself
    ],
)
def test_an_optional_file_names_the_archive(feed, name):
    """``file`` lets a manifest carry an asset name the built-in pin cannot derive."""
    feed["doc"]["runners"]["dwproton"]["file"] = name
    change, = feed["refresh"]()
    assert change.tag == NEW
    spec = runners._spec("dwproton")
    assert spec.pinned_asset == (name, 7)
    asset = runners._pinned_release(spec)["tar"]
    assert asset["name"] == name
    assert asset["browser_download_url"].endswith(f"/{NEW}/{name}")
    old, new = make_runner(OLD), make_runner(NEW)
    assert runners.installed_pinned() == new
    assert runners.is_recommended(new) and not runners.is_recommended(old)


@pytest.mark.parametrize(
    "name",
    [
        "dir/dwproton-11.0-19-x86_64.tar.xz",
        "../dwproton-11.0-19-x86_64.tar.xz",
        ".dwproton-11.0-19-x86_64.tar.xz",
        "dwproton-11.0-19..x86_64.tar.xz",
        "dwproton-11.0-19-x86_64.tar.bz2",
        "dwproton-11.0-19-x86_64",
        "a" * 201 + ".tar.xz",  # longer than 200
        "",
        7,
    ],
)
def test_a_bad_file_name_rejects_the_whole_document(feed, name):
    feed["doc"]["runners"]["dwproton"]["file"] = name
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD
    assert runners._spec("dwproton").pinned_asset == runners.RUNNERS["dwproton"].pinned_asset


def test_a_file_must_name_the_entry_tag(feed):
    # The tag picks the build; a file named for another build would hide which one it is.
    feed["doc"]["runners"]["dwproton"]["file"] = "dwproton-11.0-18-x86_64.tar.xz"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD


def test_file_cannot_redefine_the_built_in_pin(feed):
    spec = runners.RUNNERS["dwproton"]
    feed["doc"]["runners"]["dwproton"].update(
        tag=OLD,
        sha256=spec.tested[1],
        size=spec.pinned_asset[1],
        file="dwproton-11.0-17-other-x86_64.tar.xz",
    )
    assert feed["refresh"]() == []
    assert runners._spec("dwproton").pinned_asset == spec.pinned_asset


def test_file_naming_the_built_in_asset_at_the_floor_is_fine(feed):
    spec = runners.RUNNERS["dwproton"]
    feed["doc"]["runners"]["dwproton"].update(
        tag=OLD, sha256=spec.tested[1], size=spec.pinned_asset[1], file=spec.pinned_asset[0]
    )
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD


def test_a_later_epoch_cannot_repoint_an_accepted_tag(feed):
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["epoch"] += 1
    feed["doc"]["runners"]["dwproton"]["file"] = "dwproton-11.0-19-other-x86_64.tar.xz"
    assert feed["refresh"]() == []
    assert runners._spec("dwproton").pinned_asset[0] == NEW + "-x86_64.tar.xz"


def test_unknown_families_are_skipped_and_known_ones_still_apply(feed, caplog):
    """An old app must not throw away a newer manifest just because it names a new family."""
    caplog.set_level(logging.INFO)
    feed["doc"]["runners"]["steam"] = {"tag": 7, "sha256": 5}  # not even a known schema
    change, = feed["refresh"]()
    assert (change.family, change.tag) == ("dwproton", NEW)
    assert runners.pinned_tag("dwproton") == NEW
    assert sum("unknown runner families" in r.message for r in caplog.records) == 1
    feed["tick"]()
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW
    assert sum("unknown runner families" in r.message for r in caplog.records) == 1


def test_a_body_with_only_unknown_families_leaves_the_pin_alone(feed, caplog):
    caplog.set_level(logging.INFO)
    feed["doc"]["runners"] = {"steam": {"tag": "whatever"}}
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD
    assert any("unknown runner families" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "bad"),
        ("sha256", "A" * 64),  # the digest is lowercase hex, as sha256 hexdigest() writes it
        ("size", -1),
        ("size", True),
        ("tag", "../escape"),
        ("published_at", "yesterday"),
        ("published_at", "2026-11-19T00:00:00"),  # no timezone
        ("tested", True),
        ("tested", {"yes": "yes", "date": "2026-11-19", "report": "https://example.org/"}),
        ("tested", {"yes": True, "date": "2026-11-19", "report": "http://example.org/"}),
        ("critical", "yes"),
        ("min_nadare", "nonsense"),
        ("previous_tag", 7),
        ("file", "a/b.tar.xz"),
    ],
)
def test_any_malformed_entry_rejects_the_whole_document(feed, field, value):
    feed["doc"]["runners"]["ge"] = copy.deepcopy(feed["doc"]["runners"]["dwproton"])
    feed["doc"]["runners"]["ge"].update(tag="GE-Proton11-8", critical=False)
    feed["doc"]["runners"]["ge"][field] = value
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD


@pytest.mark.parametrize(
    "breaker",
    [
        lambda doc: doc["runners"]["dwproton"].pop("sha256"),
        lambda doc: doc["runners"]["dwproton"].pop("tested"),
        lambda doc: doc.update(runners=[]),
        lambda doc: doc.update(epoch=0),
    ],
)
def test_a_document_that_does_not_fit_the_schema_is_refused(feed, breaker):
    breaker(feed["doc"])
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_nadare", "99.0"),
        ("tested", {"yes": False, "date": "2026-11-19", "report": "https://example.org/test"}),
    ],
)
def test_ineligible_entry_keeps_builtin(feed, field, value):
    feed["doc"]["runners"]["dwproton"][field] = value
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == OLD


@pytest.mark.parametrize("package", ["flatpak", "appimage"])
def test_the_public_index_drives_the_pin_for_flatpak_and_appimage(feed, monkeypatch, package):
    """The index base is the one notices/catalog use; no packaging branch is involved.

    The packaging markers are set so that a Flatpak or AppImage build takes this same path.
    """
    from nadare.gui.settings_parts.extras import discover_runners

    monkeypatch.delenv("NADARE_INDEX_BASE")
    if package == "flatpak":
        monkeypatch.setenv("FLATPAK_ID", "io.github.frostlinuxdev.Nadare")
    else:
        monkeypatch.setenv("APPIMAGE", "/tmp/Nadare.AppImage")
    changes = feed["refresh"]()
    assert len(changes) == 1
    assert feed["refresh"]() == []  # announced once
    assert feed["calls"] == [
        hosting.index_base() + "runners.json",
        hosting.index_base() + "runners.json.asc",
    ]
    assert "fixture.invalid" not in feed["calls"][0]
    old, new = make_runner(OLD), make_runner(NEW)
    assert runners.pinned_tag("dwproton") == NEW
    assert runners.installed_pinned() == new
    assert runners.resolve_runner("dwproton") == new
    assert runners.is_recommended(new) and not runners.is_recommended(old)
    assert NEW in discover_runners("wine")[0][0]
    assert runners.unused_old_runners({}) == [old]
    assert runners.behind_pin({"runner": str(old)}) == (old.name, NEW)
    spec = runners._spec("dwproton")
    asset = runners._pinned_release(spec)["tar"]
    assert asset["name"] == NEW + "-x86_64.tar.xz"
    assert asset["size"] == 7
    assert f"/{NEW}/" in asset["browser_download_url"]


def test_manifest_digest_alone_refuses_corrupt_archive(feed, monkeypatch):
    feed["refresh"]()
    extracted = []
    monkeypatch.setattr(runners, "_extract", lambda *a, **kw: extracted.append(a))
    monkeypatch.setattr(runners, "_published_sha512", lambda *a: pytest.fail("sidecar trusted"))
    monkeypatch.setattr(runners.diskspace, "check_space", lambda *a: None)
    # Exercise the real downloader's verification using a local HTTP fixture response.
    import io
    monkeypatch.setattr(download, "http_open", lambda *a, **kw: io.BytesIO(b"corrupt"))
    with pytest.raises(download.DownloadError):
        runners.install("dwproton")
    assert extracted == []
    assert not runners.nadare_runners()


def test_critical_only_offers_previous_pin_and_never_edits_profiles(feed):
    from nadare.core import runnerindex

    old = make_runner(OLD)
    custom = make_runner("dwproton-11.0-18")
    profiles = {
        "default": {"backend": "wine"},
        "previous": {"backend": "wine", "runner": str(old)},
        "custom": {"backend": "wine", "runner": str(custom)},
        "own-wine": {"backend": "wine", "wine_binary": "/usr/bin/wine"},
    }
    before = copy.deepcopy(profiles)
    change, = feed["refresh"]()
    assert runnerindex.critical_offers(change, profiles) == ["previous"]
    assert profiles == before
    assert runners.resolve_runner(str(custom)) == custom
    assert runners.resolve_runner(str(old)) == old
    assert runners.prefetch_families({"custom": profiles["custom"]}) == []
    assert runners.behind_pin(profiles["previous"]) == (old.name, NEW)


def test_signed_disk_cache_is_reverified_and_lookups_never_fetch(feed):
    from nadare.core import runnerindex

    feed["refresh"]()
    calls = len(feed["calls"])
    assert runners.pinned_tag("dwproton") == NEW
    assert len(feed["calls"]) == calls
    cache = runnerindex.cache_path()
    state = json.loads(cache.read_text())
    state["accepted"]["body"] = state["accepted"]["body"].replace(NEW, "dwproton-11.0-99")
    cache.write_text(json.dumps(state))
    assert runners.pinned_tag("dwproton") == OLD


# ---- the maintainer script -------------------------------------------------------------------
SCRIPT = "scripts/make_runners_json.py"


def _run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT, *args], capture_output=True, text=True, check=False
    )


def test_maintainer_script_hashes_archive_and_prints_only_manual_sign_command(tmp_path):
    archive = tmp_path / (NEW + "-x86_64.tar.xz")
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        NEW,
        str(archive),
        "--epoch",
        "9",
        "--published-at",
        "2026-11-19T00:00:00Z",
        "--tested-date",
        "2026-11-19",
        "--test-report",
        "https://example.org/test",
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    row = json.loads(output.read_text())["runners"]["dwproton"]
    assert row["sha256"] == hashlib.sha256(b"archive").hexdigest()
    assert row["size"] == 7 and row["tag"] == NEW
    assert "file" not in row, "the derived name needs no file field"
    assert row["tested"] == {
        "yes": True, "date": "2026-11-19", "report": "https://example.org/test"
    }
    assert "--armor --detach-sign" in result.stdout
    assert releasesig.RELEASE_KEY.fingerprint in result.stdout
    assert not output.with_suffix(".json.asc").exists(), "the script must never sign"


def test_maintainer_script_rollback_keeps_other_families(tmp_path):
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({
        "epoch": 7,
        "runners": {
            "dwproton": _row(NEW),
            "ge": _row("GE-Proton11-8"),
        },
    }))
    archive = tmp_path / (OLD + "-x86_64.tar.xz")
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        OLD,
        str(archive),
        "--previous",
        str(previous),
        "--rollback",
        "--test-report",
        "https://example.org/rollback",
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(output.read_text())
    assert doc["epoch"] == 8
    assert doc["runners"]["dwproton"]["tag"] == OLD
    assert doc["runners"]["dwproton"]["previous_tag"] == NEW
    assert doc["runners"]["ge"]["tag"] == "GE-Proton11-8", "other families are kept"
    # The written body is what the app verifies: an unsigned copy is refused as before.
    assert not output.with_suffix(".json.asc").exists()


def test_maintainer_script_rollback_needs_a_tag_to_replace(tmp_path):
    """A rollback the app would refuse is an error, not a body written to the index."""
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"epoch": 7, "runners": {"ge": _row("GE-Proton11-8")}}))
    archive = tmp_path / (OLD + "-x86_64.tar.xz")
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        OLD,
        str(archive),
        "--previous",
        str(previous),
        "--rollback",
        "--test-report",
        "https://example.org/rollback",
        "--output",
        str(output),
    )
    assert result.returncode == 2
    assert "no previous tag" in result.stderr
    assert not output.exists()


def test_maintainer_script_records_a_changed_asset_name(feed, tmp_path):
    """A renamed upstream asset is written as ``file`` and the app then downloads it."""
    archive = tmp_path / "dwproton-11.0-19-nadia-x86_64.tar.zst"
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        NEW,
        str(archive),
        "--epoch",
        "1",
        "--test-report",
        "https://example.org/test",
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    row = json.loads(output.read_text())["runners"]["dwproton"]
    assert row["file"] == archive.name
    feed["doc"] = json.loads(output.read_text())
    assert feed["refresh"]()
    assert runners._spec("dwproton").pinned_asset == (archive.name, 7)


def test_maintainer_script_refuses_an_unsafe_asset_name(tmp_path):
    folder = tmp_path / "sub"
    folder.mkdir()
    archive = folder / "dwproton-11.0-19-x86_64.tar.zip"
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        NEW, str(archive), "--epoch", "1", "--test-report", "https://example.org/test",
        "--output", str(output),
    )
    assert result.returncode == 2
    assert not output.exists()


def test_maintainer_script_warns_when_the_pin_is_below_the_built_in_floor(tmp_path):
    """Such a body is legal to write but every released Nadare refuses it: say so."""
    older = "dwproton-11.0-16"
    archive = tmp_path / (older + "-x86_64.tar.xz")
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        older,
        str(archive),
        "--epoch",
        "2",
        "--test-report",
        "https://example.org/test",
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    assert "older than Nadare's built-in pin" in result.stderr
    assert json.loads(output.read_text())["runners"]["dwproton"]["tag"] == older


def test_the_new_pin_is_logged_once_and_prefetch_reports_it(feed, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    seen = []
    monkeypatch.setattr(runners, "prefetch_families", lambda profiles: [])
    assert runners.prefetch({"g": {"backend": "wine"}}, on_change=seen.append) == []
    assert [[change.tag for change in changes] for changes in seen] == [[NEW]]
    assert sum(
        record.message == f"Recommended runner updated to {NEW}" for record in caplog.records
    ) == 1
    caplog.clear()
    feed["tick"]()
    assert feed["refresh"]() == []
    assert not any("Recommended runner updated" in record.message for record in caplog.records)


def test_a_lower_epoch_is_refused_even_with_a_newer_tag(feed):
    feed["doc"]["epoch"] = 5
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["epoch"] = 4
    feed["doc"]["runners"]["dwproton"]["tag"] = "dwproton-11.0-20"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW


def test_a_changed_body_at_the_same_epoch_is_refused(feed):
    feed["doc"]["epoch"] = 5
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["runners"]["dwproton"]["tag"] = "dwproton-11.0-20"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW


@pytest.mark.parametrize("mode", ["omitted", "untested", "requires_new_app"])
def test_later_manifest_cannot_silently_restore_the_floor(feed, mode):
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["epoch"] += 1
    row = feed["doc"]["runners"]["dwproton"]
    if mode == "omitted":
        feed["doc"]["runners"] = {"ge": _row("GE-Proton11-8")}
    elif mode == "untested":
        row["tested"]["yes"] = False
    else:
        row["min_nadare"] = "99.0"
    assert feed["refresh"]() == []
    assert runners.pinned_tag("dwproton") == NEW


@pytest.mark.parametrize("field,value", [("sha256", "f" * 64), ("size", 8)])
def test_later_epoch_cannot_redefine_an_accepted_tag(feed, field, value):
    assert feed["refresh"]()
    feed["tick"]()
    feed["doc"]["epoch"] += 1
    feed["doc"]["runners"]["dwproton"][field] = value
    assert feed["refresh"]() == []
    assert runners._spec("dwproton").tested[1] == hashlib.sha256(b"archive").hexdigest()
    assert runners._spec("dwproton").pinned_asset[1] == 7


def test_signed_rollback_can_restore_the_exact_builtin_pin(feed):
    assert feed["refresh"]()
    feed["tick"]()
    spec = runners.RUNNERS["dwproton"]
    feed["doc"]["epoch"] += 1
    feed["doc"]["runners"]["dwproton"].update(
        tag=OLD, previous_tag=NEW, sha256=spec.tested[1], size=spec.pinned_asset[1]
    )
    change, = feed["refresh"]()
    assert (change.previous_tag, change.tag) == (NEW, OLD)
    assert runners.pinned_tag("dwproton") == OLD


@pytest.mark.parametrize("family,tag", [
    ("dwproton", NEW), ("ge", "GE-Proton11-8"), ("cachyos", "cachyos-11.0-20260704-slr"),
])
def test_maintainer_output_is_accepted_and_sign_command_selects_release_key(
    feed, tmp_path, family, tag,
):
    import shlex

    spec = runners.RUNNERS[family]
    folder = tmp_path / "path with spaces"
    folder.mkdir()
    archive = folder / spec.pinned_asset[0].replace(spec.tested[0], tag)
    archive.write_bytes(b"archive")
    output = folder / "runners.json"
    result = _run_script(tag, str(archive), "--family", family, "--epoch", "1",
                         "--test-report", "https://example.org/test", "--output", str(output))
    assert result.returncode == 0, result.stderr
    command = shlex.split(next(line.strip() for line in result.stdout.splitlines()
                              if line.strip().startswith("gpg ")))
    assert command[command.index("--local-user") + 1] == releasesig.RELEASE_KEY.fingerprint
    assert command[-1] == str(output)
    assert command[command.index("--output") + 1] == str(output) + ".asc"
    feed["doc"] = json.loads(output.read_text())
    assert feed["refresh"]()
    assert runners.pinned_tag(family) == tag


def test_maintainer_script_refuses_an_archive_named_for_another_tag(tmp_path):
    """The app rejects a file that does not name its tag; the script must not write one."""
    archive = tmp_path / "dwproton-11.0-18-x86_64.tar.xz"
    archive.write_bytes(b"archive")
    output = tmp_path / "runners.json"
    result = _run_script(
        NEW, str(archive), "--epoch", "9", "--test-report", "https://example.org/t",
        "--output", str(output),
    )
    assert result.returncode == 2, result.stderr
    assert "tag" in result.stderr
    assert not output.exists()
