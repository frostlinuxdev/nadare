# SPDX-License-Identifier: GPL-3.0-or-later
"""Arknights/Stella Sora explain a blocked or failed umu runtime like CZN does (#429)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import runtimewatch, umu, userns
from nadare.core.backends import BackendError

BWRAP = (
    "pressure-vessel-wrap[1778]: E: Child process exited with code 1: "
    "bwrap: setting up uid map: Permission denied"
)
UMU_DOWN = "RuntimeError: umu has not been setup for the user"


# ---- the shared watcher ----------------------------------------------------------
def test_watch_starts_clean_and_leaves_messages_alone() -> None:
    watch = runtimewatch.RuntimeWatch()
    assert watch.userns_blocked is False
    assert watch.umu_setup_failed is False
    assert watch.explain("prefix creation failed (exit 1)") == "prefix creation failed (exit 1)"


def test_watch_flags_each_failure_from_the_output() -> None:
    watch = runtimewatch.RuntimeWatch()
    watch.feed("INFO: steamrt4 is up to date")
    assert (watch.userns_blocked, watch.umu_setup_failed) == (False, False)
    watch.feed(BWRAP)
    assert (watch.userns_blocked, watch.umu_setup_failed) == (True, False)
    watch.feed(UMU_DOWN)
    assert (watch.userns_blocked, watch.umu_setup_failed) == (True, True)


def test_watch_explain_appends_the_matching_hints() -> None:
    blocked = runtimewatch.RuntimeWatch()
    blocked.feed(BWRAP)
    out = blocked.explain("failed")
    assert out.startswith("failed") and userns.hint() in out and umu.SETUP_HINT not in out

    down = runtimewatch.RuntimeWatch()
    down.feed(UMU_DOWN)
    out = down.explain("failed")
    assert umu.SETUP_HINT in out and userns.hint() not in out

    both = runtimewatch.RuntimeWatch()
    both.feed(BWRAP)
    both.feed(UMU_DOWN)
    out = both.explain("failed")
    assert userns.hint() in out and umu.SETUP_HINT in out


# ---- Arknights / Stella Sora -----------------------------------------------------
def _backend(tmp_path: Path) -> ArknightsBackend:
    backend = ArknightsBackend(make_context(tmp_path, ak.KIND, {}, "ak"))
    backend.ctx.emit = lambda event: None
    return backend


def _stream_that(monkeypatch, lines: list[str], code: int) -> None:
    def fake(argv, env, on_line=None, cancel=None, **kw):
        for line in lines:
            if on_line is not None:
                on_line(line)
        return code

    monkeypatch.setattr(wg, "stream", fake)
    monkeypatch.setattr(
        ArknightsBackend, "_prefix_command", lambda self, args, extra_env=None: (["x"], {})
    )


def test_prefix_failure_names_apparmor_when_bwrap_was_blocked(tmp_path, monkeypatch) -> None:
    backend = _backend(tmp_path)
    _stream_that(monkeypatch, [BWRAP], 1)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert "prefix creation failed (exit 1)" in str(err.value)
    assert userns.has_hint(str(err.value))


def test_prefix_failure_explains_a_failed_umu_download(tmp_path, monkeypatch) -> None:
    backend = _backend(tmp_path)
    _stream_that(monkeypatch, [UMU_DOWN], 0)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert "prefix creation failed (exit 0)" in str(err.value)
    assert umu.SETUP_HINT in str(err.value)


def test_prefix_failure_is_unchanged_for_other_errors(tmp_path, monkeypatch) -> None:
    backend = _backend(tmp_path)
    _stream_that(monkeypatch, ["wine: something unrelated"], 1)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert str(err.value) == "prefix creation failed (exit 1)"


def test_installer_that_never_started_is_explained(tmp_path, monkeypatch) -> None:
    backend = _backend(tmp_path)
    installer = write_exe(tmp_path / "Arknights-setup.exe", "MZ")
    _stream_that(monkeypatch, [BWRAP], 1)
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
    backend.ctx.profile["installer_url"] = (
        "https://launcher-pkg-ark-en.yo-star.com/Arknights-setup.exe"
    )
    backend.ctx.profile["wine_binary"] = "/usr/bin/wine"
    with pytest.raises(BackendError) as err:
        backend.install()
    assert "did not finish" in str(err.value)
    assert userns.has_hint(str(err.value))


def test_launcher_exit_after_a_block_is_explained(tmp_path, monkeypatch) -> None:
    backend = _backend(tmp_path)
    write_exe(backend.install_dir() / "game launcher.exe", "MZ")

    def fake_launch(self) -> int:
        self._watch.feed(BWRAP)
        return 1

    monkeypatch.setattr(WineGameBackend, "launch", fake_launch)
    with pytest.raises(BackendError) as err:
        backend.update()
    assert "launcher exited with code 1" in str(err.value)
    assert userns.has_hint(str(err.value))
