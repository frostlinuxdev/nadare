# SPDX-License-Identifier: GPL-3.0-or-later
"""The CZN PCSDK stub ships as a Go build (#113, #119); the C build stays as a fallback.

The Go stub (stub/pcsdk-go) is memory-safe and hardened per #119. For one release the C
stub is still shipped and can be chosen with the profile key ``pcsdk_stub = "c"`` or
``NADARE_CZN_STUB=c``, in case a game update breaks the Go one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _parts_patch import patch_everywhere

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core import report

REPO = Path(__file__).resolve().parents[1]
GO_SHA256 = "0a8cc27d2b6a7eb5097d65ca04214de997693da34483e11bc5d76211caddbdf7"
C_SHA256 = "fe4a5bad970750e974cf999e31e3b87132fbb25f9c04c9eea69e3acc2e243185"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# what ships
# ---------------------------------------------------------------------------
def test_the_default_shipped_stub_is_the_go_build():
    assert czn_backend.STUB_PREBUILT_SHA256 == GO_SHA256
    assert _sha(czn_backend.STUB_PREBUILT.read_bytes()) == GO_SHA256


def test_the_c_stub_still_ships_beside_it():
    assert czn_backend.STUB_C_PREBUILT.name == "czn_pcsdk_stub_c.exe"
    assert czn_backend.STUB_C_PREBUILT_SHA256 == C_SHA256
    assert _sha(czn_backend.STUB_C_PREBUILT.read_bytes()) == C_SHA256


def test_the_report_knows_both_shipped_stubs():
    assert set(report.STUB_SHIPPED_SHA256) == {GO_SHA256, C_SHA256}


def test_the_go_build_is_pinned_to_a_container_digest():
    script = (REPO / "scripts" / "build_stub_go.sh").read_text(encoding="utf-8")
    assert "golang@sha256:" in script
    assert "-trimpath" in script and "-buildid=" in script


def test_ci_rebuilds_both_stubs_and_compares_them():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/build_stub_go.sh dist/czn_pcsdk_stub.exe" in ci
    assert "scripts/build_stub_go.sh --test" in ci
    assert "cmp src/nadare/assets/czn_pcsdk_stub.exe dist/czn_pcsdk_stub.exe" in ci
    assert "cmp src/nadare/assets/czn_pcsdk_stub_c.exe dist/czn_pcsdk_stub_c.exe" in ci


# ---------------------------------------------------------------------------
# choosing a stub
# ---------------------------------------------------------------------------
@pytest.fixture
def make_backend_context(tmp_path):
    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


GO = b"MZ-go-stub"
C = b"MZ-c-stub"


@pytest.fixture
def shipped(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "czn_pcsdk_stub.exe").write_bytes(GO)
    (assets / "czn_pcsdk_stub_c.exe").write_bytes(C)
    patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT", assets / "czn_pcsdk_stub.exe")
    patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT_SHA256", _sha(GO))
    patch_everywhere(monkeypatch, czn_backend, "STUB_C_PREBUILT", assets / "czn_pcsdk_stub_c.exe")
    patch_everywhere(monkeypatch, czn_backend, "STUB_C_PREBUILT_SHA256", _sha(C))
    monkeypatch.delenv("NADARE_CZN_STUB", raising=False)


def _backend(make_backend_context, tmp_path, monkeypatch, **settings):
    ctx = make_backend_context("stove-czn", {"prefix": str(tmp_path / "prefix"), **settings})
    backend = CznBackend(ctx)
    messages: list[str] = []
    monkeypatch.setattr(backend, "info", lambda message, **data: messages.append(message))
    return backend, messages


def test_a_new_profile_gets_the_go_stub(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch)
    assert backend._ensure_stub().read_bytes() == GO


def test_the_profile_can_choose_the_c_stub(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    assert backend._ensure_stub().read_bytes() == C


def test_the_environment_can_choose_the_c_stub(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    monkeypatch.setenv("NADARE_CZN_STUB", "c")
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch)
    assert backend._ensure_stub().read_bytes() == C


def test_an_unknown_choice_means_the_go_stub(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="rust")
    assert backend._ensure_stub().read_bytes() == GO


def test_an_old_c_stub_is_swapped_for_the_go_one_and_kept(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    """Every current CZN profile holds the C stub; the update moves it to Go."""
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch)
    stub = backend._stub()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(C)
    assert backend._ensure_stub().read_bytes() == GO
    assert stub.with_name(stub.name + ".old").read_bytes() == C


def test_switching_back_to_c_replaces_the_go_stub(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch)
    stub = backend._ensure_stub()
    assert stub.read_bytes() == GO
    backend_c, _ = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    assert backend_c._ensure_stub().read_bytes() == C


def test_the_launch_log_names_which_stub_runs(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    backend._ensure_stub()
    assert any("PCSDK stub" in m and "(C)" in m for m in messages)
    backend_go, messages_go = _backend(make_backend_context, tmp_path / "2", monkeypatch)
    backend_go._ensure_stub()
    assert any("PCSDK stub" in m and "(Go)" in m for m in messages_go)


def test_stub_identity_counts_either_shipped_stub(shipped, tmp_path):
    path = tmp_path / "stub.exe"
    path.write_bytes(C)
    assert czn_backend.stub_identity(path) == (_sha(C)[:12], True)
    path.write_bytes(b"other")
    assert czn_backend.stub_identity(path)[1] is False


def test_the_stub_build_script_path_is_beside_the_backend():
    """#1248: the code that runs build_stub.sh moved to backend_parts/; the path must not move."""
    import inspect

    from nadare.backends.czn import backend as czn_backend_mod
    from nadare.backends.czn.backend_parts import prereqs

    assert prereqs._MODULE_FILE == Path(czn_backend_mod.__file__).resolve()
    assert (prereqs._MODULE_FILE.with_name("build_stub.sh")).is_file()
    assert 'Path(_MODULE_FILE).with_name("build_stub.sh")' in inspect.getsource(prereqs)
