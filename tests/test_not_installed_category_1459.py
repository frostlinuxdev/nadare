# SPDX-License-Identifier: GPL-3.0-or-later
"""Update on a game that is not installed raises the category the recovery reads (#1459).

The CLI and GUI offer "install" when they see ``BackendNotInstalled``, not when the
English message says so; a raise site that drops back to a plain ``BackendError``
would silently lose that guidance.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context
from _official_route import OfficialRouteBackend
from _parts_patch import patch_everywhere

from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.direct import BrownDust2Backend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core.backends import BackendNotInstalled

BACKENDS = (
    ("bd2", BrownDust2Backend),
    ("nikke", NikkeBackend),
    ("arknights-direct", ArknightsBackend),
    ("stellasora-direct", StellaSoraBackend),
    # The shared WineGameBackend.update() path (no registered kind uses it unchanged today).
    ("official-route", OfficialRouteBackend),
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(wg.vcredist, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(wg.runners, "installed_pinned", lambda family="dwproton": None)

    def offline(*_a, **_k):
        raise wg.runners.RunnerError("offline (no downloads in tests)")

    monkeypatch.setattr(wg.runners, "ensure_pinned", offline)
    patch_everywhere(monkeypatch, wg, "scan_pids", lambda patterns: [])


@pytest.mark.parametrize(("name", "cls"), BACKENDS, ids=[row[0] for row in BACKENDS])
def test_update_without_the_game_raises_not_installed(tmp_path, name, cls):
    values = {}
    if issubclass(cls, ArknightsBackend):
        values["manifest_file"] = str(tmp_path / "manifest.json")
    backend = cls(make_context(tmp_path, cls.kind, values, name))
    with pytest.raises(BackendNotInstalled):
        backend.update()
