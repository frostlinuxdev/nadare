# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the CZN/backends robustness triage batch.

Covers: #53 (auth worker non-object JSON), #52 (update --apply dir/file
manifest conflict), #34 (malformed shortcuts.vdf), #40 (CZN profile path
expansion), #41 (hidden .old-* runner selection), #59 (_prune_stale guard).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import auth as czn_auth
from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn import runners as czn_runners
from nadare.backends.czn import update as czn_update
from nadare.backends.czn.backend import CznBackend
from nadare.core import runners as core_runners
from nadare.core import shortcuts


def _czn_backend(tmp_path: Path, profile: dict) -> CznBackend:
    return CznBackend(make_context(tmp_path, "stove-czn", profile))


# ---------------------------------------------------------------------------
# #53 — a non-object JSON body must raise RuntimeError, not AttributeError
# ---------------------------------------------------------------------------
class _ArrayResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def json(self):
        return ["not", "an", "object"]


class _StringResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def json(self):
        return "not an object"


def test_parse_rejects_a_non_object_json_body():
    with pytest.raises(RuntimeError):
        czn_auth.StoveAuth._parse(None, _ArrayResponse(), "tag")
    with pytest.raises(RuntimeError):
        czn_auth.StoveAuth._parse(None, _StringResponse(), "tag")


def test_login_qr_tolerates_a_non_object_status_value(monkeypatch, tmp_path):
    import base64

    class _Auth:
        def qr_create(self):
            image = base64.b64encode(b"PNG").decode()
            return {"session": "s", "url": "u", "image": image}

        def qr_status(self, session):
            return {"code": 0, "value": "not-a-dict"}

    monkeypatch.setattr(czn_auth, "say", lambda *a, **k: None)
    monkeypatch.setattr(czn_auth, "event", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="timed out"):
        czn_auth.StoveAuth.login_qr(
            _Auth(), timeout_s=1, png_path=tmp_path / "qr.png"
        )


# ---------------------------------------------------------------------------
# #52 — a directory entry colliding with an existing file is a clean error
# ---------------------------------------------------------------------------
def test_update_apply_reports_a_directory_conflict(monkeypatch, tmp_path, capsys):
    game = tmp_path / "game"
    game.mkdir()
    (game / "collide").write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(czn_update, "GAME_DIR", game)
    monkeypatch.setattr(czn_update, "diskspace", None)
    monkeypatch.setattr(czn_update, "local_version", lambda: 1)
    monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (2, "test"))
    monkeypatch.setattr(czn_update, "fetch_manifest", lambda version: {"version_no": 2})
    monkeypatch.setattr(czn_update, "game_running", lambda: False)
    monkeypatch.setattr(
        czn_update,
        "plan",
        lambda manifest: ([("/f.bin", "1", "c0", "a" * 32, "b" * 32, 1)], ["/collide"]),
    )

    assert czn_update.main(["update", "--apply"]) == 1
    out = capsys.readouterr().out
    assert "/collide" in out


# ---------------------------------------------------------------------------
# #34 — a truncated/corrupt shortcuts.vdf degrades to []
# ---------------------------------------------------------------------------
_HEADER = b"\x00shortcuts\x00"


def test_vdf_loads_truncated_string_returns_empty():
    data = _HEADER + b"\x00" + b"outer\x00" + b"\x01" + b"inner\x00" + b"trunc"
    assert shortcuts.vdf_loads(data) == []


def test_vdf_loads_truncated_int_returns_empty():
    data = _HEADER + b"\x00" + b"outer\x00" + b"\x02" + b"inner\x00" + b"\x01\x00"
    assert shortcuts.vdf_loads(data) == []


def test_add_steam_shortcut_refuses_a_corrupt_vdf(monkeypatch, tmp_path):
    """A file that cannot be parsed back must never be rewritten (it would drop
    every shortcut the user already has)."""
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    bad = _HEADER + b"\x00" + b"outer\x00" + b"\x02" + b"inner\x00" + b"\x01\x00"
    vdf.write_bytes(bad)
    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.add_steam_shortcut(vdf, "p", "T", ["/opt/n"])
    assert vdf.read_bytes() == bad


# ---------------------------------------------------------------------------
# #40 — CZN prefix/runner profile paths are expanded
# ---------------------------------------------------------------------------
def test_prefix_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    backend = _czn_backend(tmp_path, {"prefix": str(tmp_path / "p")})
    backend.ctx.profile["prefix"] = "~/Games/czn"
    assert backend.prefix() == tmp_path / "Games" / "czn"


def test_resolve_runner_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    proton = tmp_path / "MyProton"
    proton.mkdir()
    (proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    backend = _czn_backend(tmp_path, {"prefix": str(tmp_path / "p")})
    backend.ctx.profile["runner"] = "~/MyProton"
    assert backend.resolve_runner() == (str(proton), "proton")


def test_resolve_runner_resolves_an_installed_ge_name(monkeypatch, tmp_path):
    root = tmp_path / "runners"
    found = root / "GE-Proton11-7"
    found.mkdir(parents=True)
    (found / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
    backend = _czn_backend(tmp_path, {"prefix": str(tmp_path / "p")})
    backend.ctx.profile["runner"] = "GE-Proton11-7"
    assert backend.resolve_runner() == (str(found), "proton")


# ---------------------------------------------------------------------------
# #41 — hidden .old-* crash leftovers are never selected
# ---------------------------------------------------------------------------
def test_installed_runner_skips_hidden_leftovers(monkeypatch, tmp_path):
    root = tmp_path / "runners"
    real = root / "GE-Proton11-7"
    real.mkdir(parents=True)
    (real / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    leftover = root / ".GE-Proton11-7.old-dead1234"
    leftover.mkdir(parents=True)
    (leftover / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(czn_backend.paths, "runners_dir", lambda: root)
    backend = _czn_backend(tmp_path, {"prefix": str(tmp_path / "p")})
    assert backend._installed_runner("ge") == real


def test_czn_installed_skips_hidden_leftovers(monkeypatch, tmp_path):
    root = tmp_path / "runners"
    real = root / "GE-Proton11-7"
    real.mkdir(parents=True)
    (real / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    leftover = root / ".GE-Proton11-7.old-deadbeef"
    leftover.mkdir(parents=True)
    (leftover / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(czn_runners, "RUNNERS_DIR", root)
    assert czn_runners.installed() == [real]


# ---------------------------------------------------------------------------
# #59 — _prune_stale must not delete a live installer's .old-* backup
# ---------------------------------------------------------------------------
def test_prune_stale_keeps_a_live_old_backup(tmp_path):
    backup = tmp_path / ".GE-Proton11-7.old-abc"
    backup.mkdir()
    (backup / core_runners._OWNER_MARKER).write_text(str(os.getpid()), encoding="utf-8")
    core_runners._prune_stale(tmp_path)
    assert backup.exists()


def test_prune_stale_removes_an_unowned_old_backup(tmp_path):
    stale = tmp_path / ".GE-Proton11-7.old-deadbeef"
    stale.mkdir()
    core_runners._prune_stale(tmp_path)
    assert not stale.exists()
