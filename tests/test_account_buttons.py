# SPDX-License-Identifier: GPL-3.0-or-later
"""Log in / Log out say what can be done now (b23-rc2 click-through, NIKKE not installed:
both were enabled; Log in could only fail with "install it first", Log out had nothing)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.backends import Status
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _view():
    view = GameView()
    view.set_profile("nikke", {"backend": "launcher-nikke"})
    view.set_account_controls(True)
    return view


def test_log_in_waits_for_the_install_when_the_backend_says_so(qapp):
    view = _view()
    try:
        view.set_status(Status("not_installed", data={"logged_in": False, "can_login": False}))
        assert not view.login.isEnabled()
        assert "install" in view.login.toolTip().lower()
        view.set_status(Status("installed", data={"logged_in": False, "can_login": True}))
        assert view.login.isEnabled()
    finally:
        view.deleteLater()


def test_log_out_needs_a_sign_in_when_the_backend_knows(qapp):
    view = _view()
    try:
        view.set_status(Status("installed", data={"logged_in": False}))
        assert not view.logout.isEnabled()
        view.set_status(Status("installed", data={"logged_in": True}))
        assert view.logout.isEnabled()
        view.set_status(Status("not_installed"))  # unknown: stays reachable (#338)
        assert view.logout.isEnabled()
    finally:
        view.deleteLater()


def test_nikke_status_says_log_in_needs_the_install(tmp_path):
    from _backend_helpers import make_context

    from nadare.backends.launcher import NikkeBackend

    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix")})
    )
    data = backend.status().data
    assert data.get("can_login") is False


def test_log_out_stays_for_an_expired_saved_login(qapp):
    """Round 3: an expired saved Netmarble login is exactly what Log out must clear."""
    view = _view()
    try:
        view.set_status(Status("installed", data={"logged_in": False, "login_expired": True}))
        assert view.logout.isEnabled()
    finally:
        view.deleteLater()


def test_an_empty_sign_in_file_is_not_logged_in(tmp_path):
    from _backend_helpers import make_context

    from nadare.backends.launcher import NikkeBackend
    from nadare.backends.nikke import hosts as h

    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix")})
    )
    storage = h.cache_storage_dir(backend.prefix())
    storage.mkdir(parents=True)
    (storage / "intl_auth.txt").write_bytes(b"")
    assert backend.status().data.get("logged_in") is False


def test_the_button_names_the_sign_in_the_backend_uses(qapp):
    """Maintainer, 2026-11-11: NIKKE's Log in only does Google, so the button says so."""
    view = _view()
    try:
        view.set_status(
            Status("installed", data={"logged_in": False, "login_label": "Log in with Google"})
        )
        assert view.login.text() == "Log in with Google"
        view.set_status(Status("installed", data={"logged_in": False}))
        assert view.login.text() == "Log in"  # every other backend keeps the plain label
    finally:
        view.deleteLater()


def test_nikke_status_says_log_in_is_google(tmp_path):
    from _backend_helpers import make_context

    from nadare.backends.launcher import NikkeBackend

    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix")})
    )
    assert backend.status().data.get("login_label") == "Log in with Google"
