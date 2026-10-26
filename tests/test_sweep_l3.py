# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the L3 robustness sweep (findings #333/#334/#338/#339/#361)."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.czn.backend import CznBackend
from nadare.core import download as dl
from nadare.core import vcredist
from nadare.core.backends import BackendError
from nadare.core.config import TOP_DEFAULTS, Config
from nadare.core.events import Cancel


# ---------------------------------------------------------------------------
# #333 F-10: saving through a symlink keeps the link
# ---------------------------------------------------------------------------
def test_save_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "config.json"
    real.write_text(json.dumps({"profiles": {}}), encoding="utf-8")

    link_dir = tmp_path / "config-home"
    link_dir.mkdir()
    link = link_dir / "config.json"
    link.symlink_to(real)

    cfg = Config.load(link)
    cfg.data["check_updates"] = False
    cfg.save()

    assert link.is_symlink()
    assert link.resolve() == real
    payload = json.loads(real.read_text(encoding="utf-8"))
    assert payload["check_updates"] is False
    assert list(real_dir.glob(".config-*.tmp")) == []
    assert list(link_dir.glob(".config-*.tmp")) == []


# ---------------------------------------------------------------------------
# #333 F-11: hand-edited booleans are not truthy strings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("true", True),
        ("0", False),
        ("1", True),
        (0, False),
        (1, True),
        ("maybe", TOP_DEFAULTS["check_updates"]),
        (None, TOP_DEFAULTS["check_updates"]),
    ],
)
def test_top_level_boolean_strings_are_coerced(
    tmp_path: Path, raw: object, expected: bool
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"check_updates": raw}), encoding="utf-8")
    assert Config.load(path).data["check_updates"] is expected


def test_known_boolean_defaults_are_all_coerced(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "check_updates": "false",
                "lock_to_single_instance": "0",
                "telemetry": "1",
            }
        ),
        encoding="utf-8",
    )
    data = Config.load(path).data
    assert data["check_updates"] is False
    assert data["lock_to_single_instance"] is False
    assert data["telemetry"] is True


# ---------------------------------------------------------------------------
# #334 F-06: a failed replace leaves the old file and no temp behind
# ---------------------------------------------------------------------------
def test_failed_save_keeps_the_original_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    original = json.dumps({"profiles": {}, "check_updates": True})
    path.write_text(original, encoding="utf-8")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr("nadare.core.config.os.replace", boom)
    cfg = Config.load(path)
    cfg.data["check_updates"] = False

    with pytest.raises(OSError):
        cfg.save()

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config-*.tmp")) == []


# ---------------------------------------------------------------------------
# #334 F-08: an invalid-UTF-8 config is quarantined, load returns defaults
# ---------------------------------------------------------------------------
def test_invalid_utf8_config_is_quarantined_with_a_notice(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b'{"check_updates": false}\xff\xfe')

    cfg = Config.load(path)

    assert cfg.data["check_updates"] == TOP_DEFAULTS["check_updates"]
    assert cfg.profiles == {}
    assert cfg.notice, "the user must be told the settings were reset"
    assert not path.exists()
    assert list(tmp_path.glob("config.json.corrupt-*"))


# ---------------------------------------------------------------------------
# #334 F-05: disk/write errors are not retried as network failures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (errno.ENOSPC, "not enough disk space"),
        (errno.EACCES, "cannot write to"),
        (errno.EROFS, "cannot write to"),
    ],
)
def test_download_does_not_retry_unwritable_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, fragment: str
) -> None:
    calls = {"n": 0}

    def fail(*_a: object, **_k: object) -> None:
        calls["n"] += 1
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(dl, "http_open", fail)
    monkeypatch.setattr(dl, "_sleep", lambda *_a, **_k: None)
    dest = tmp_path / "game.bin"

    with pytest.raises(dl.DownloadError, match=fragment):
        dl.download("http://example.invalid/game", dest, retries=3, backoff=0)

    assert calls["n"] == 1, "a write failure must not be retried"
    assert not dest.exists()


# ---------------------------------------------------------------------------
# #361 F-10: VC++ verification failures are plain language, hashes in the log
# ---------------------------------------------------------------------------
def test_vcredist_hash_failure_is_plain_and_logs_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "vc_redist.x64.exe"
    path.write_bytes(b"MZ-not-the-pin")

    with caplog.at_level("WARNING", logger="nadare.core.vcredist"):
        with pytest.raises(BackendError) as exc:
            vcredist.verify(
                path,
                profile={},
                emit=lambda _e: None,
                cancel=Cancel(),
                constant="0" * 64,
                index_pins_fn=lambda: set(),
            )

    assert exc.value.message == (
        "Could not verify the Microsoft Visual C++ download. "
        "Check your connection and try again."
    )
    assert "did not match any accepted hash" in caplog.text
    assert "pinned sha2" in caplog.text
    assert not path.exists()


def test_vcredist_ensure_status_is_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = (
        "Could not verify the Microsoft Visual C++ download. "
        "Check your connection and try again."
    )

    def fail(*_a: object, **_k: object) -> None:
        raise BackendError(message)

    monkeypatch.setattr(vcredist, "install", fail)
    events: list = []
    vcredist.ensure(
        tmp_path / "prefix",
        profile={},
        cache_dir=tmp_path / "cache",
        run_in_prefix=lambda _argv: 0,
        emit=events.append,
        cancel=Cancel(),
    )
    statuses = [e.message for e in events if e.kind == "status"]
    assert statuses == [message]


# ---------------------------------------------------------------------------
# #339 F-07: a fresh official install runs the VC++ step once
# ---------------------------------------------------------------------------
def test_official_install_runs_vcredist_once_on_a_fresh_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ArknightsBackend(make_context(tmp_path, ak.KIND, {}, "fresh"))
    calls: list[int] = []

    def ensure_prefix(self) -> bool:
        calls.append(1)  # _ensure_prefix runs the VC++ step itself
        return True

    installer = tmp_path / "setup.exe"
    installer.write_text("MZ")
    monkeypatch.setattr(ArknightsBackend, "_ensure_default_runner", lambda *a, **k: None)
    monkeypatch.setattr(ArknightsBackend, "_ensure_prefix", ensure_prefix)
    monkeypatch.setattr(ArknightsBackend, "_run_vcredist", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(
        ArknightsBackend,
        "require_installer_url",
        lambda *a, **k: "https://example.invalid/setup.exe",
    )
    monkeypatch.setattr(ArknightsBackend, "_download_installer", lambda *a, **k: installer)
    monkeypatch.setattr(ak, "stream", lambda *a, **k: 0)
    # CI runners have no wine; the test must not depend on one being installed.
    monkeypatch.setattr(ak, "first_wine", lambda: Path("/usr/bin/wine"))
    monkeypatch.setattr(ArknightsBackend, "find_launcher", lambda self: installer)
    backend.ctx.emit = lambda _e: None

    backend.install()

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# #338 F-09: network failure and rejection read differently
# ---------------------------------------------------------------------------
def test_stove_network_and_rejection_have_different_messages(tmp_path: Path) -> None:
    backend = CznBackend(make_context(tmp_path, "stove-czn", {}, "stove"))

    with pytest.raises(BackendError) as network:
        backend._fail_auth("login", 4)
    assert "Could not reach STOVE" in network.value.message

    with pytest.raises(BackendError) as rejected:
        backend._fail_auth("login", 3)
    assert "rejected" in rejected.value.message
    assert "Could not reach STOVE" not in rejected.value.message


# ---------------------------------------------------------------------------
# #333 F-12: an unusable single-instance socket prints a next step
# ---------------------------------------------------------------------------
def test_unusable_single_instance_lock_prints_the_setting(capsys) -> None:
    from nadare.gui import EXIT_NO_SINGLE_INSTANCE, _single_instance_exit

    class _Instance:
        status = "could not create the single-instance lock '/run/x.sock'"

    assert _single_instance_exit(_Instance()) == EXIT_NO_SINGLE_INSTANCE
    err = capsys.readouterr().err
    assert "lock_to_single_instance" in err
    assert "/run/x.sock" in err
