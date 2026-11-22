# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""#1598: with Remember me off, a backend object takes over a token another one rotated.

The GUI builds a fresh backend per operation. STOVE rotates the refresh token on every renewal,
so a backend built before another one renewed holds a dead token. With Remember me on the
keyring carries the new one across; with it off only the in-process live session does, and
``_adopt_newer_credential`` used to return early and ignore it.
"""

from __future__ import annotations

from _czn_test_helpers import (  # noqa: F401 - pytest fixtures, found by name
    _cred_backend,
    _credential_blob,
    _reset_czn_process_state,
    fake_keyring,
    make_backend_context,
)


def test_remember_off_adopts_the_token_another_backend_rotated(
    make_backend_context, tmp_path, fake_keyring
):
    first = _cred_backend(make_backend_context, tmp_path, remember=False)
    first._save_credential("token-1", "42")
    stale = _cred_backend(make_backend_context, tmp_path, remember=False)
    assert stale._refresh_token == "token-1"

    # Another operation renews: STOVE hands back a rotated token.
    renewer = _cred_backend(make_backend_context, tmp_path, remember=False)
    renewer._save_credential("token-2", "42")

    with stale._rotation_lock(wait=1):
        stale._adopt_newer_credential()

    assert stale._refresh_token == "token-2"
    assert stale._member_no == "42"
    assert fake_keyring.store == {}


def test_remember_off_with_no_live_session_keeps_its_own_token(
    make_backend_context, tmp_path, fake_keyring
):
    backend = _cred_backend(make_backend_context, tmp_path, remember=False)
    backend._refresh_token = "mine"
    backend._member_no = "42"

    backend._adopt_newer_credential()

    assert backend._refresh_token == "mine"


def test_remember_on_still_prefers_the_keyring_over_the_live_session(
    make_backend_context, tmp_path, fake_keyring
):
    backend = _cred_backend(make_backend_context, tmp_path, remember=True)
    backend._save_credential("token-1", "42")
    # Another process rotated and saved to the keyring; this process's live copy is older.
    fake_keyring.store[next(iter(fake_keyring.store))] = (
        '{"refresh_token":"token-from-other-process","member_no":"42"}'
    )
    backend._adopt_newer_credential()

    assert backend._refresh_token == "token-from-other-process"
    assert _credential_blob(fake_keyring)["refresh_token"] == "token-from-other-process"

