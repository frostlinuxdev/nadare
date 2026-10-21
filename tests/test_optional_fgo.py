# SPDX-License-Identifier: GPL-3.0-or-later
"""A build without ``nadare.backends.fgo`` must still run (Flathub / no-FGO bundle)."""

from __future__ import annotations

import argparse
import sys

import pytest

from nadare.core import backends
from nadare.core.config import Config


@pytest.fixture
def no_fgo(monkeypatch):
    """Make every ``nadare.backends.fgo*`` import fail, as if left out of the bundle."""
    for name in list(sys.modules):
        if name == "nadare.backends.fgo" or name.startswith("nadare.backends.fgo."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "nadare.backends.fgo", None)
    monkeypatch.setattr(
        backends,
        "_REGISTRY",
        {k: v for k, v in backends._REGISTRY.items() if k != "fgo-lepton"},
    )


def test_hosting_has_no_lepton_url():
    # lepton_url lives with the FGO installer now, not in core.
    from nadare.core import hosting

    assert not hasattr(hosting, "lepton_url")
    assert "lepton_url" not in hosting.__all__


def test_installer_url_still_built_from_index(monkeypatch):
    from nadare.backends.fgo import installer

    monkeypatch.setenv("NADARE_INDEX_BASE", "https://example.test/idx/")
    assert installer.installer_url().startswith("https://example.test/idx/lepton/")
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    assert installer.installer_url() == ""


def test_available_kinds_excludes_missing_fgo(no_fgo):
    assert "fgo-lepton" in backends.unavailable()
    assert backends.is_available("wine")
    assert not backends.is_available("fgo-lepton")


def test_available_kinds_includes_fgo_when_present():
    assert backends.is_available("fgo-lepton")


def test_doctor_fgo_section_without_fgo_does_not_crash(no_fgo, capsys):
    from nadare import app

    config = Config()
    args = argparse.Namespace(fgo=True)
    app._print_fgo_readiness(config, args)
    out = capsys.readouterr().out
    assert "not included" in out.lower()


def test_doctor_without_fgo_flag_prints_nothing(no_fgo, capsys):
    from nadare import app

    app._print_fgo_readiness(Config(), argparse.Namespace(fgo=False))
    assert capsys.readouterr().out == ""


def test_add_game_catalog_hides_unavailable(no_fgo):
    pytest.importorskip("PyQt6")
    from nadare.gui import add_game

    kinds = [g.kind for g in add_game.available_catalog()]
    assert "fgo-lepton" not in kinds
    assert "wine" in kinds or "stove-czn" in kinds


def test_add_game_catalog_shows_fgo_when_present():
    pytest.importorskip("PyQt6")
    from nadare.gui import add_game

    assert "fgo-lepton" in [g.kind for g in add_game.available_catalog()]
