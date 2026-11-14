# SPDX-License-Identifier: GPL-3.0-or-later
"""Without /usr/bin/python3 the launch must name the real cause, not "check your connection"."""

from __future__ import annotations

import pytest
from test_umu_always import _proton

from nadare.core import umu
from nadare.core.backends import BackendError


def test_missing_shebang_python_is_named_in_the_launch_error(tmp_path, monkeypatch) -> None:
    backend, _proton_dir = _proton(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(umu, "managed", lambda: None)
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "missing-python3"))
    monkeypatch.setattr(umu, "BUNDLED_ARCHIVE", tmp_path / "no-bundled-umu.tar")
    with pytest.raises(BackendError) as caught:
        backend.build_launch()
    # UNAVAILABLE_HINT talks about the internet and umu-launcher; the actual blocker here is
    # the interpreter the managed zipapp's shebang names.
    assert "python3" in str(caught.value)
