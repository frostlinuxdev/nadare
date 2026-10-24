# SPDX-License-Identifier: GPL-3.0-or-later
"""umu cannot download its runtime (Valve's server down / offline) is explained (#429 follow-up)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import umu
from nadare.core.backends import BackendError

SETUP_FAILED = "RuntimeError: umu has not been setup for the user"
NEEDS_NET = "An internet connection is required to setup umu"


def test_setup_failure_lines_are_recognised() -> None:
    assert umu.is_setup_failure_line(SETUP_FAILED)
    assert umu.is_setup_failure_line(NEEDS_NET)
    assert umu.is_setup_failure_line("[log] Runt…rror<12>: umu has not been setup for the user")


def test_other_umu_output_is_not_a_setup_failure() -> None:
    assert not umu.is_setup_failure_line("")
    assert not umu.is_setup_failure_line("INFO: steamrt4 is up to date")
    assert not umu.is_setup_failure_line("pressure-vessel-wrap[1]: E: bwrap: setting up uid map")


def test_setup_hint_names_the_server_and_the_next_step() -> None:
    text = umu.SETUP_HINT
    assert "repo.steampowered.com" in text
    assert "try again" in text.lower()
    assert "GPU" not in text


def _czn(tmp_path: Path):
    from _backend_helpers import make_context

    from nadare.backends.czn.backend import CznBackend

    ctx = make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")}, "test")
    return CznBackend(ctx)


def test_czn_flags_a_failed_umu_setup(tmp_path: Path) -> None:
    backend = _czn(tmp_path)
    assert backend._watch.umu_setup_failed is False
    backend._emit_text("INFO: steamrt4 is up to date")
    assert backend._watch.umu_setup_failed is False
    backend._emit_text(NEEDS_NET)
    assert backend._watch.umu_setup_failed is True


def test_czn_prefix_failure_explains_a_failed_umu_setup(tmp_path: Path, monkeypatch) -> None:
    backend = _czn(tmp_path)

    def fake_run(args):
        backend._emit_text(SETUP_FAILED)
        return 0  # umu exits 0 after this failure; the old message said "(exit 0)" only

    monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert "prefix creation failed (exit 0)" in str(err.value)
    assert umu.SETUP_HINT in str(err.value)


def test_czn_webview2_failure_explains_a_failed_umu_setup(tmp_path: Path, monkeypatch) -> None:
    backend = _czn(tmp_path)
    backend._emit_text(NEEDS_NET)
    assert umu.SETUP_HINT in backend._explain("WebView2 Runtime install failed")


def test_czn_message_is_unchanged_when_nothing_failed(tmp_path: Path) -> None:
    backend = _czn(tmp_path)
    assert backend._explain("prefix creation failed (exit 1)") == "prefix creation failed (exit 1)"


# ---- a runner whose Steam Runtime this umu does not know ---------------------------
MISMATCH = (
    "ValueError: Failed to match "
    "'/home/nadare/.local/share/nadare/runners/dwproton-11.0-15-x86_64' "
    "with a container runtime"
)


def test_runtime_mismatch_line_is_recognised() -> None:
    assert umu.is_runtime_mismatch_line(MISMATCH)
    ellipsised = "[log] Valu…rror<10>: Failed to match '/x' with a container runtime"
    assert umu.is_runtime_mismatch_line(ellipsised)
    assert not umu.is_runtime_mismatch_line("")
    assert not umu.is_runtime_mismatch_line("INFO: steamrt4 is up to date")
    assert not umu.is_runtime_mismatch_line("Failed to match nothing")


def test_runtime_mismatch_hint_says_what_to_update() -> None:
    text = umu.RUNTIME_MISMATCH_HINT
    assert "umu" in text and "newer" in text
    assert "Update" in text
    assert "Traceback" not in text


def test_runtimewatch_flags_and_explains_the_mismatch() -> None:
    from nadare.core.runtimewatch import RuntimeWatch

    watch = RuntimeWatch()
    watch.feed("INFO: steamrt4 is up to date")
    assert watch.umu_runtime_mismatch is False
    assert watch.explain("prefix creation failed (exit 1)") == "prefix creation failed (exit 1)"
    watch.feed(MISMATCH)
    assert watch.umu_runtime_mismatch is True
    assert umu.RUNTIME_MISMATCH_HINT in watch.explain("prefix creation failed (exit 1)")


def test_czn_explains_a_runtime_mismatch(tmp_path: Path) -> None:
    backend = _czn(tmp_path)
    backend._emit_text(MISMATCH)
    assert umu.RUNTIME_MISMATCH_HINT in backend._explain("prefix creation failed (exit 1)")
