# SPDX-License-Identifier: GPL-3.0-or-later
"""A finished predownload is ready only for the plan it fetched (#1497).

WuWa's HD and UHD tiers share one version but need different files. Readiness used to be keyed
on the version alone, so switching the quality (or a same-version manifest change) kept saying
"ready" and disabled Predownload for files that were never fetched. All offline: plans and the
download are fakes.
"""

from __future__ import annotations

import json

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct
from nadare.core import directdl as dd

MANIFESTS = {
    ("hd", 1): "aa" * 16,
    ("uhd", 1): "bb" * 16,
    ("hd", 2): "cc" * 16,  # same version, the publisher re-published HD
}


class World:
    manifest = 1
    offline = False
    fetched: list[tuple[str, str]] = []


def _plan_for(quality: str, token: str | None = None) -> dd.Plan:
    if World.offline:
        raise dd.DirectError("offline")
    md5 = MANIFESTS[(quality, World.manifest)]
    url = f"https://cdn.example/{quality}/tier.pak" + (f"?t={token}" if token else "")
    files = (dd.RemoteFile(f"Client/Content/{quality.upper()}/tier.pak", url, size=4, md5=md5),)
    return dd.Plan("wuwa", "2.0", files=files, exe="Wuthering Waves.exe")


@pytest.fixture
def wuwa(tmp_path, monkeypatch):
    World.manifest, World.offline, World.fetched = 1, False, []

    def factory(fetch=None, *, quality="hd", token=None):
        return _plan_for(quality, token)

    def fake_predownload(plan, dest, cache, *, emit=None, cancel=None, workers=4, files=None):
        World.fetched.append((plan.version, plan.files[0].path))
        cache.mkdir(parents=True, exist_ok=True)
        return {"bytes": 4}

    monkeypatch.setattr(direct.WutheringWavesBackend, "predownload_factory", staticmethod(factory))
    monkeypatch.setattr(dd, "predownload", fake_predownload)
    backend = direct.WutheringWavesBackend(
        make_context(
            tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "pfx"), "wuwa_quality": "hd"}
        )
    )
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file("1.0", "wuwa")
    backend._write_latest("1.0")
    return backend


def _ready(backend) -> object:
    data, _, _ = backend._direct_status_parts(backend.direct_target(), False)
    return data.get("predownload_ready")


def _refresh(backend) -> None:
    path = backend.install_dir() / ".nadare" / "predownload.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["checked_at"] = 0  # expire the check interval
    path.write_text(json.dumps(state), encoding="utf-8")
    backend._refresh_predownload_state()


def test_a_finished_predownload_is_ready(wuwa):
    wuwa.predownload()
    assert _ready(wuwa) is True


def test_the_same_choice_stays_ready_after_a_refresh(wuwa):
    wuwa.predownload()
    _refresh(wuwa)
    assert _ready(wuwa) is True


def test_switching_the_quality_is_not_ready_at_once_even_offline(wuwa):
    wuwa.predownload()
    World.offline = True
    wuwa.ctx.set("wuwa_quality", "uhd")
    assert _ready(wuwa) is False, "UHD was never fetched but the page says ready"
    _refresh(wuwa)  # the refresh fails offline and keeps the old receipt
    assert _ready(wuwa) is False


def test_switching_the_quality_is_not_ready_after_a_refresh(wuwa):
    wuwa.predownload()
    wuwa.ctx.set("wuwa_quality", "uhd")
    _refresh(wuwa)
    assert _ready(wuwa) is False
    wuwa.predownload()  # the button is offered again and fetches the UHD files
    assert World.fetched[-1][1].startswith("Client/Content/UHD/")
    assert _ready(wuwa) is True


def test_switching_back_to_the_fetched_quality_is_ready_again(wuwa):
    wuwa.predownload()
    wuwa.ctx.set("wuwa_quality", "uhd")
    assert _ready(wuwa) is False
    wuwa.ctx.set("wuwa_quality", "hd")
    assert _ready(wuwa) is True


def test_a_same_version_manifest_change_is_not_ready(wuwa):
    wuwa.predownload()
    World.manifest = 2  # same version 2.0, different files
    _refresh(wuwa)
    assert _ready(wuwa) is False


def test_a_rotating_download_address_does_not_drop_readiness(wuwa, monkeypatch):
    """Signed URLs change between fetches; only what the files are counts."""
    wuwa.predownload()
    monkeypatch.setattr(
        direct.WutheringWavesBackend,
        "predownload_factory",
        staticmethod(lambda fetch=None, *, quality="hd", token=None: _plan_for(quality, "rotated")),
    )
    _refresh(wuwa)
    assert _ready(wuwa) is True


def test_a_receipt_without_an_identity_is_not_trusted(wuwa):
    """A version-only receipt (written before #1497) cannot say which files it fetched."""
    wuwa._write_predownload_state("2.0", True)
    path = wuwa.install_dir() / ".nadare" / "predownload.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for key in ("plan", "selection"):
        state.pop(key, None)
    path.write_text(json.dumps(state), encoding="utf-8")
    assert _ready(wuwa) is False
    _refresh(wuwa)
    assert _ready(wuwa) is False


def test_the_download_token_is_never_written_to_the_receipt(wuwa):
    wuwa.ctx.set("kuro_token", "SECRET-TOKEN-1497")
    wuwa.predownload()
    text = (wuwa.install_dir() / ".nadare" / "predownload.json").read_text(encoding="utf-8")
    assert "SECRET-TOKEN-1497" not in text
    assert _ready(wuwa) is True
    wuwa.ctx.set("kuro_token", "ANOTHER")
    assert _ready(wuwa) is False  # another token is another selection


def test_a_closed_window_still_clears_the_notice(wuwa, monkeypatch):
    wuwa.predownload()
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "predownload_factory", staticmethod(lambda *a, **k: None)
    )
    _refresh(wuwa)
    data, _, _ = wuwa._direct_status_parts(wuwa.direct_target(), False)
    assert "predownload_version" not in data


# --- receipts written by 0.1.0b24 (version only) ------------------------------------------------
from nadare.core import sophon  # noqa: E402


def _legacy(backend, version: str = "2.0", checked_at: int | None = None) -> None:
    """A receipt exactly as 0.1.0b24 wrote it."""
    import time

    path = backend.install_dir() / ".nadare" / "predownload.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": version,
                "checked_at": int(time.time()) if checked_at is None else checked_at,
                "ready": True,
            }
        ),
        encoding="utf-8",
    )


def _zzz(tmp_path, monkeypatch, pkg: sophon.SophonPackage):
    plan = dd.Plan("zzz", "3.4.0", exe="ZenlessZoneZero.exe", sophon=(pkg,))
    monkeypatch.setattr(
        direct.ZenlessBackend, "predownload_factory", staticmethod(lambda f=None: plan)
    )
    backend = direct.ZenlessBackend(
        make_context(tmp_path, "direct-zzz", {"prefix": str(tmp_path / "p")})
    )
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file("3.3.0", "zzz")
    backend._write_latest("3.3.0")
    return backend


def test_a_b24_hoyo_receipt_is_rechecked_at_once_against_its_chunk_markers(tmp_path, monkeypatch):
    pkg = sophon.SophonPackage("zzz-3.4.0", "https://x/m", "https://x/c", "m340", 1)
    backend = _zzz(tmp_path, monkeypatch, pkg)
    cache = backend._predownload_cache("3.4.0")
    cache.mkdir(parents=True)
    sophon._predownload_ready_path(pkg, cache).write_text("m340", encoding="utf-8")
    _legacy(backend, "3.4.0")  # checked just now: the interval would normally skip the check
    backend._refresh_predownload_state()
    assert _ready(backend) is True
    assert backend._read_predownload_state()["plan"]  # upgraded to the new receipt


def test_a_b24_hoyo_receipt_without_its_chunks_is_not_ready(tmp_path, monkeypatch):
    pkg = sophon.SophonPackage("zzz-3.4.0", "https://x/m", "https://x/c", "m340", 1)
    backend = _zzz(tmp_path, monkeypatch, pkg)
    _legacy(backend, "3.4.0")
    backend._refresh_predownload_state()
    assert _ready(backend) is False


def test_a_b24_kuro_receipt_is_rechecked_at_once_and_offered_again(wuwa):
    """Kuro's own marker records only the version, so the quality cannot be proven."""
    _legacy(wuwa)
    wuwa._refresh_predownload_state()
    assert _ready(wuwa) is False
    assert wuwa._read_predownload_state()["plan"]


def test_a_receipt_with_a_selection_but_no_plan_is_not_ready(wuwa):
    """Hand-edited or half-written: without the plan it cannot say what it fetched."""
    wuwa.predownload()
    path = wuwa.install_dir() / ".nadare" / "predownload.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("plan")
    path.write_text(json.dumps(state), encoding="utf-8")
    assert _ready(wuwa) is False


@pytest.mark.parametrize("marker", [None, "m339"])
def test_a_b24_hoyo_cache_without_the_right_marker_is_not_ready(tmp_path, monkeypatch, marker):
    pkg = sophon.SophonPackage("zzz-3.4.0", "https://x/m", "https://x/c", "m340", 1)
    backend = _zzz(tmp_path, monkeypatch, pkg)
    cache = backend._predownload_cache("3.4.0")
    cache.mkdir(parents=True)
    (cache / "chunk").write_bytes(b"c")
    if marker:
        sophon._predownload_ready_path(pkg, cache).write_text(marker, encoding="utf-8")
    _legacy(backend, "3.4.0")
    backend._refresh_predownload_state()
    assert _ready(backend) is False
