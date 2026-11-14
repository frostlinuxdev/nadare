# SPDX-License-Identifier: GPL-3.0-or-later
"""W2-D3: CZN Repair must fix the failures Verify names before saying all is well."""
# ruff: noqa: F811 - tests take the fixtures imported below as arguments

from __future__ import annotations

from _czn_test_helpers import _install_game, _make_backend, make_backend_context  # noqa: F401


def test_repair_fixes_the_stub_and_webview2_it_reported(
    make_backend_context, tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    _install_game(prefix)
    backend = _make_backend(make_backend_context, prefix)

    before = backend.verify()
    assert before["failed"] == ["stub", "webview2"], before

    fixed: list[str] = []
    monkeypatch.setattr(backend, "_running", lambda: False)
    monkeypatch.setattr(backend, "update", lambda full=False: None)
    monkeypatch.setattr(backend, "_differing_files", lambda: [])
    monkeypatch.setattr(backend, "_ensure_stub", lambda: fixed.append("stub"))
    monkeypatch.setattr(backend, "_ensure_webview2", lambda: fixed.append("webview2"))
    backend.repair()
    assert fixed == ["stub", "webview2"], (before, fixed)
