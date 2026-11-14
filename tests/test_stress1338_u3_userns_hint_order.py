# SPDX-License-Identifier: GPL-3.0-or-later
"""A blocked Steam Runtime must reach the GUI's fix dialog even when umu also fails (#1338).

`launching.py` asks `umu.resolve(...)` for the managed copy before it checks
`userns.blocks_runtime()`. When resolution fails (source install, offline, no system umu),
the launch raises `umu.UNAVAILABLE_HINT` first, without `userns.hint()`. The GUI only opens the
one-time AppArmor fix dialog for a message carrying that hint, so on an Ubuntu host the user is
sent to check their internet instead of being told about the block that stops every launch.
"""

from __future__ import annotations

import pytest
from test_backends_arknights import _backend, _install_launcher

from nadare.backends import winegame as wg
from nadare.core import umu, userns
from nadare.core.backends import BackendError


def test_a_blocked_runtime_is_reported_even_when_umu_cannot_be_set_up(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _install_launcher(prefix)
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: None)
    monkeypatch.setattr(userns, "blocks_runtime", lambda: True)

    backend = _backend(tmp_path, {"prefix": str(prefix), "runner": "MyProton"})
    with pytest.raises(BackendError) as caught:
        backend.build_launch()
    assert userns.has_hint(str(caught.value)), str(caught.value)
