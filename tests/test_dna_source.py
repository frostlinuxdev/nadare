# SPDX-License-Identifier: GPL-3.0-or-later
"""Duet Night Abyss: the full client from the publisher's CDN, no launcher (#1330)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core.sources import dna

FIX = Path(__file__).parent / "fixtures"
PKG = (
    "https://pan01-cdn-ali-jp.dna-panstudio.com/Packages/Global/WindowsNoEditor/PC_OBT_Global_Pub/"
)


def _routes(manifest=None, md5doc=None):
    manifest = manifest or json.loads((FIX / "catalog/dna_version_manifest.json").read_bytes())
    md5doc = md5doc or json.loads((FIX / "catalog/dna_hpatch_md5.json").read_bytes())
    return {
        PKG + "VersionManifest.json": json.dumps(manifest).encode(),
        PKG + "1.7/17002/full_17002/HPatchDiffMd5.json": json.dumps(md5doc).encode(),
    }


class Fetch:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def __call__(self, url, headers=None):
        self.urls.append(url)
        if url not in self.routes:
            raise dd.DirectError(f"HTTP 404 for {url}")
        return self.routes[url]


def test_plan_is_one_full_diff():
    fetch = Fetch(_routes())
    plan = dna.resolve(fetch)
    assert plan.game_tag == "dna"
    assert plan.version == "1.7 (17002)"
    assert plan.exe == "EM.exe"
    assert plan.files == ()
    (arc,) = plan.archives
    assert arc.unwrap == ("hdiff",)
    assert arc.slot == "client"
    assert arc.source.url == PKG + "1.7/17002/full_17002/full_17002.hdiff"
    assert arc.source.md5 == "1dda90a2938ed9cc61fc23588fd8003f"
    assert arc.source.size == 28376496394
    assert plan.extra["installed_size"] == 30967047498
    assert all(u.startswith("https://") for u in fetch.urls)


@pytest.mark.parametrize(
    "change",
    [
        {"latest_version": "../17002"},
        {"latest_version": ""},
        {"latest_version": 17002},
        {"latest_version_number": "1.7/.."},
        {"latest_version_number": None},
    ],
)
def test_a_bad_version_manifest_is_refused(change):
    manifest = json.loads((FIX / "catalog/dna_version_manifest.json").read_bytes())
    manifest.update(change)
    with pytest.raises(dd.DirectError):
        dna.resolve(Fetch(_routes(manifest=manifest)))


@pytest.mark.parametrize(
    "change",
    [
        {"name": "../evil.hdiff"},
        {"name": "full_17002.zip"},
        {"md5": "nope"},
        {"size": -1},
        {"size": "28376496394"},
        {"size": True},
    ],
)
def test_a_bad_diff_entry_is_refused(change):
    md5doc = json.loads((FIX / "catalog/dna_hpatch_md5.json").read_bytes())
    md5doc["hdiff_file"].update(change)
    with pytest.raises(dd.DirectError):
        dna.resolve(Fetch(_routes(md5doc=md5doc)))


def test_a_cdn_that_fails_falls_through_to_the_next(monkeypatch):
    routes = _routes()
    other = "https://pan01-cdn-aws-jp.dna-panstudio.com/"
    moved = {
        k.replace("https://pan01-cdn-ali-jp.dna-panstudio.com/", other): v
        for k, v in routes.items()
    }
    monkeypatch.setattr(dna, "CDNS", ("https://pan01-cdn-ali-jp.dna-panstudio.com/", other))
    plan = dna.resolve(Fetch(moved))
    assert plan.archives[0].source.url.startswith(other)


# ----------------------------------------------------------------- directdl
def test_directdl_installs_an_hdiff_archive_and_skips_it_next_time(tmp_path):
    body = (FIX / "hdiff/full_small.hdiff").read_bytes()
    s = Server({"/full.hdiff": body})
    try:
        arc = dd.Archive(
            "full.hdiff",
            dd.RemoteFile(
                "full.hdiff", s.url + "/full.hdiff", len(body), md5=hashlib.md5(body).hexdigest()
            ),
            unwrap=("hdiff",),
            slot="client",
        )
        plan = dd.Plan("dna", "1", archives=(arc,), exe="EM.exe")
        dest = tmp_path / "game"
        assert dd.sync(plan, dest) == {"downloaded": 1, "skipped": 0}
        assert (dest / "EM.exe").read_bytes() == b"MZ fake stub"
        assert (dest / "EM/Content/Paks/empty.pak").is_file()
        assert not (dest / dd.DL_DIR).exists()  # the 28 GB diff is not kept after unpacking
        n = len(s.requests)
        assert dd.sync(plan, dest) == {"downloaded": 0, "skipped": 1}
        assert len(s.requests) == n
    finally:
        s.close()


def test_directdl_refuses_an_hdiff_with_the_wrong_md5(tmp_path):
    body = (FIX / "hdiff/full_small.hdiff").read_bytes()
    s = Server({"/full.hdiff": body})
    try:
        arc = dd.Archive(
            "full.hdiff",
            dd.RemoteFile("full.hdiff", s.url + "/full.hdiff", len(body), md5="0" * 32),
            unwrap=("hdiff",),
        )
        with pytest.raises(dd.DirectError):
            dd.sync(dd.Plan("dna", "1", archives=(arc,)), tmp_path / "game")
        assert not (tmp_path / "game/EM.exe").exists()
    finally:
        s.close()


# ------------------------------------------------------------------ backend
def test_backend_is_a_catalog_direct_kind_that_requires_the_sandbox():
    from nadare.backends.direct import DnaBackend
    from nadare.core import backends, directkinds, profilenames

    assert DnaBackend.kind == "direct-dna"
    assert "direct-dna" in directkinds.DIRECT_KINDS
    assert directkinds.tier_of("direct-dna") == directkinds.TIER_EXPERIMENTAL
    assert profilenames.SHORT_NAMES["direct-dna"] == "dna"
    assert backends.sandbox_required("direct-dna") is True
    assert DnaBackend.account_notice and "Anti-Cheat Expert" in DnaBackend.account_notice
    # every other kind is unchanged
    assert not any(backends.sandbox_required(k) for k in directkinds.CATALOG_KINDS - {"direct-dna"})


def test_required_sandbox_ignores_a_saved_off():
    from nadare.core import gamesandbox

    off = {"secure_mode": False}
    assert gamesandbox.wanted(off, supported=True, required=True) is True
    assert gamesandbox.wanted(off, supported=True, required=False) is False
    assert gamesandbox.wanted(off, supported=False, required=True) is False


def test_required_sandbox_never_falls_back_to_an_unprotected_start(tmp_path, monkeypatch):
    """No bubblewrap here: an on-by-default game would start unprotected; DNA must not."""
    from _backend_helpers import make_context

    from nadare.backends.direct import DnaBackend
    from nadare.core import gamesandbox
    from nadare.core.backends import BackendError

    monkeypatch.setattr(
        gamesandbox, "locate", lambda **_: gamesandbox.Located(None, found=False, why="none")
    )
    backend = DnaBackend(make_context(tmp_path, "direct-dna", {"secure_mode": False}))
    assert backend.secure_mode_enabled() is True
    with pytest.raises(BackendError, match="bubblewrap"):
        backend._sandbox_check("game")


def test_sandbox_switch_is_locked_on_but_still_shows_protection():
    from nadare.core.gamefacts import SandboxFacts

    facts = SandboxFacts(
        supported=True, chosen=True, locked="This game always runs in the Sandbox."
    )
    assert not facts.editable()
    assert facts.pill() == "SANDBOX"
    assert facts.tooltip("x") == "This game always runs in the Sandbox."


def test_the_space_check_counts_the_diff_and_its_files_together():
    """The diff (28.4 GB) and the files it unpacks to (31 GB) are on disk at the same time."""
    from nadare.backends.direct import DnaBackend

    doc = json.loads((FIX / "catalog/dna_hpatch_md5.json").read_bytes())
    assert DnaBackend.install_size_hint >= doc["hdiff_file"]["size"] + doc["new_size"]
