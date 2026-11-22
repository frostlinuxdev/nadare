# SPDX-License-Identifier: GPL-3.0-or-later
"""The CZN PCSDK stub ships as a Go build (#113, #119); the C fallback is retired (#1552).

The Go stub (stub/pcsdk-go) is memory-safe and hardened per #119. The C stub shipped as a
fallback through 0.1.0b25; a profile or environment that still asks for it
(``pcsdk_stub = "c"`` / ``NADARE_CZN_STUB=c``) gets the Go stub and one log line.
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
from nadare.core.backends import BackendError

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


def test_the_c_stub_is_retired():
    """#1552: the C fallback, its source and its build scripts are gone."""
    assert not (REPO / "src/nadare/assets/czn_pcsdk_stub_c.exe").exists()
    assert not (REPO / "src/nadare/backends/czn/stub_src").exists()
    assert not (REPO / "src/nadare/backends/czn/build_stub.sh").exists()
    assert not (REPO / "scripts/build_stub.sh").exists()
    assert not hasattr(czn_backend, "STUB_C_PREBUILT")
    assert not hasattr(czn_backend, "STUB_C_NAME")
    spec = (REPO / "packaging/nadare.spec").read_text(encoding="utf-8")
    assert "stub_src" not in spec and "build_stub.sh" not in spec
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "build_stub.sh" not in pyproject


def test_the_report_knows_only_the_go_stub():
    assert set(report.STUB_SHIPPED_SHA256) == {GO_SHA256}


def test_the_go_build_is_pinned_to_a_container_digest():
    script = (REPO / "scripts" / "build_stub_go.sh").read_text(encoding="utf-8")
    assert "golang@sha256:" in script
    assert "-trimpath" in script and "-buildid=" in script


@pytest.mark.parametrize("rel", [".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"])
def test_ci_rebuilds_the_go_stub_and_compares_it(rel):
    ci = (REPO / rel).read_text(encoding="utf-8")
    assert "scripts/build_stub_go.sh dist/czn_pcsdk_stub.exe" in ci
    assert "scripts/build_stub_go.sh --test" in ci
    assert "cmp src/nadare/assets/czn_pcsdk_stub.exe dist/czn_pcsdk_stub.exe" in ci
    assert "build_stub.sh --container" not in ci
    assert "czn_pcsdk_stub_c" not in ci


# ---------------------------------------------------------------------------
# choosing a stub
# ---------------------------------------------------------------------------
@pytest.fixture
def make_backend_context(tmp_path):
    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


GO = b"MZ-go-stub"
OLD_C = b"MZ-c-stub"


@pytest.fixture
def shipped(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "czn_pcsdk_stub.exe").write_bytes(GO)
    patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT", assets / "czn_pcsdk_stub.exe")
    patch_everywhere(monkeypatch, czn_backend, "STUB_PREBUILT_SHA256", _sha(GO))
    patch_everywhere(monkeypatch, czn_backend, "RETIRED_C_STUB_SHA256", _sha(OLD_C))
    monkeypatch.delenv("NADARE_CZN_STUB", raising=False)
    return assets


def _backend(make_backend_context, tmp_path, monkeypatch, **settings):
    ctx = make_backend_context("stove-czn", {"prefix": str(tmp_path / "prefix"), **settings})
    backend = CznBackend(ctx)
    messages: list[str] = []
    monkeypatch.setattr(backend, "info", lambda message, **data: messages.append(message))
    return backend, messages


def test_the_retired_c_digest_is_the_old_shipped_one():
    assert czn_backend.RETIRED_C_STUB_SHA256 == C_SHA256


def test_a_new_profile_gets_the_go_stub(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch)
    assert backend._ensure_stub().read_bytes() == GO


def test_a_profile_that_chose_c_gets_the_go_stub_and_one_log_line(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    assert backend._ensure_stub().read_bytes() == GO
    retired = [m for m in messages if "retired" in m.lower()]
    assert len(retired) == 1, messages
    assert "pcsdk_stub" in retired[0]


def test_the_environment_choosing_c_gets_the_go_stub_and_one_log_line(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    monkeypatch.setenv("NADARE_CZN_STUB", "c")
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch)
    assert backend._ensure_stub().read_bytes() == GO
    retired = [m for m in messages if "retired" in m.lower()]
    assert len(retired) == 1, messages
    assert "NADARE_CZN_STUB" in retired[0]


def test_no_choice_logs_nothing_about_the_c_stub(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="rust")
    assert backend._ensure_stub().read_bytes() == GO
    assert not [m for m in messages if "retired" in m.lower()]


def test_an_old_c_stub_is_swapped_for_the_go_one_and_kept(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    """A profile still holding the C stub (or set to it) moves to Go; the old copy stays."""
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    stub = backend._stub()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(OLD_C)
    assert backend._ensure_stub().read_bytes() == GO
    assert stub.with_name(stub.name + ".old").read_bytes() == OLD_C
    assert any("(retired C)" in m for m in messages), messages


def test_the_launch_log_names_the_go_stub(shipped, make_backend_context, tmp_path, monkeypatch):
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch)
    backend._ensure_stub()
    assert any("PCSDK stub" in m and "(Go)" in m for m in messages)


def test_stub_identity_counts_only_the_go_stub(shipped, tmp_path):
    path = tmp_path / "stub.exe"
    path.write_bytes(GO)
    assert czn_backend.stub_identity(path) == (_sha(GO)[:12], True)
    path.write_bytes(OLD_C)
    assert czn_backend.stub_identity(path)[1] is False
    path.write_bytes(b"other")
    assert czn_backend.stub_identity(path)[1] is False


def test_a_missing_shipped_stub_is_a_clear_error_not_a_build(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    """With the C source gone there is nothing to build: say what is wrong instead."""
    (shipped / "czn_pcsdk_stub.exe").unlink()
    backend, _ = _backend(make_backend_context, tmp_path, monkeypatch)
    ran: list = []
    monkeypatch.setattr(backend, "_stream", lambda *a, **k: ran.append(a) or 0)
    with pytest.raises(BackendError, match="(?i)reinstall"):
        backend._ensure_stub()
    assert ran == []
    assert not backend._stub().exists()


def test_the_ignored_notice_is_logged_once_per_profile(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    """A later install/update/launch must not repeat the same "setting is ignored" line."""
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    assert backend._ensure_stub().read_bytes() == GO
    assert len([m for m in messages if "is ignored" in m]) == 1

    later, later_messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    later._ensure_stub()
    assert not [m for m in later_messages if "is ignored" in m], later_messages


def test_a_missing_go_stub_with_an_old_c_stub_errors_and_never_claims_go(
    shipped, make_backend_context, tmp_path, monkeypatch
):
    """BUG 5.1: the swap failed, so do not say "the Go stub runs" and do not keep the C one."""
    (shipped / "czn_pcsdk_stub.exe").unlink()
    backend, messages = _backend(make_backend_context, tmp_path, monkeypatch, pcsdk_stub="c")
    stub = backend._stub()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(OLD_C)

    with pytest.raises(BackendError, match="(?i)reinstall"):
        backend._ensure_stub()

    assert not [m for m in messages if "the Go stub runs" in m], messages
    assert [m for m in messages if "could not be installed" in m], messages
    assert stub.read_bytes() == OLD_C  # the unusable C stub was restored, not lost
