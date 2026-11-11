# SPDX-License-Identifier: GPL-3.0-or-later
"""#991 point 3: an interrupted first install says how much is already on disk."""

from __future__ import annotations

import pytest
from test_winegame_status_characterization import DIRECT_BACKENDS, _backend, _install

from nadare.core import diskspace


@pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
def test_an_interrupted_install_reports_its_size(tmp_path, name, cls, direct):
    backend = _backend(tmp_path, name, cls, direct)
    _install(backend)
    (backend.install_dir() / "data.bin").write_bytes(b"x" * 3_000_000)
    backend._install_marker_path().write_text("sync in progress\n", encoding="utf-8")
    status = backend.status()
    size = diskspace.tree_bytes(backend.install_dir())
    assert status.data["partial_bytes"] == size
    assert diskspace.human_size(size) in status.message
    assert "RESUME" in status.message and "remove" in status.message.lower()


@pytest.mark.parametrize(("name", "cls", "direct"), DIRECT_BACKENDS)
def test_an_interrupted_update_does_not_offer_to_delete_a_playable_game(
    tmp_path, name, cls, direct
):
    backend = _backend(tmp_path, name, cls, direct)
    _install(backend)
    backend._write_install_marker(("1.0", "1.1"))
    status = backend.status()
    assert "partial_bytes" not in status.data
    assert "interrupted" in status.message and "remove" not in status.message.lower()
